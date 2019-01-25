from firedrake.preconditioners.base import PCBase, SNESBase
from firedrake.petsc import PETSc
from firedrake.solving_utils import _SNESContext
from firedrake.matrix_free.operators import ImplicitMatrixContext
from firedrake.dmhooks import get_appctx, push_appctx, pop_appctx

from collections import namedtuple
import operator
from functools import partial
import numpy
import operator
from ufl import VectorElement, MixedElement, Coefficient, FunctionSpace
from tsfc.kernel_interface.firedrake import KernelBuilder as FiredrakeKernelBuilder

from pyop2 import op2
from pyop2 import base as pyop2
from pyop2 import sequential as seq
from pyop2.datatypes import IntType

__all__ = ("PatchPC", "PlaneSmoother", "PatchSNES")


class PatchKernelBuilder(FiredrakeKernelBuilder):
    """Custom kernel interface for patch assembly.

    Ensures that a provided set of coefficients are not split apart if they are mixed.

    This is necessary because PETSc provides the state vector (which
    may be mixed) as one concatenated vector, rather than the
    Firedrake convention of one vector per subspace."""
    def __init__(self, *unsplit_coefficients):
        self.unsplit_coefficients = frozenset(unsplit_coefficients)
        self.KernelBuilder = self

    # TSFC makes the kernel builder by calling "interface.KernelBuilder(...)"
    # on the provided interface object. So mock that with this __call__ method.
    def __call__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        return self

    def set_coefficients(self, integral_data, form_data):
        """Prepare the coefficients of the form.

        :arg integral_data: UFL integral data
        :arg form_data: UFL form data
        """
        coefficients = []
        coefficient_numbers = []
        # enabled_coefficients is a boolean array that indicates which
        # of reduced_coefficients the integral requires.
        for i in range(len(integral_data.enabled_coefficients)):
            if integral_data.enabled_coefficients[i]:
                orig = form_data.reduced_coefficients[i]
                coefficient = form_data.function_replace_map[orig]
                if type(coefficient.ufl_element()) == MixedElement:
                    if orig in self.unsplit_coefficients:
                        coefficients.append(coefficient)
                        self.coefficient_split[coefficient] = [coefficient]
                    else:
                        split = [Coefficient(FunctionSpace(coefficient.ufl_domain(), element))
                                 for element in coefficient.ufl_element().sub_elements()]
                        coefficients.extend(split)
                        self.coefficient_split[coefficient] = split
                else:
                    coefficients.append(coefficient)
                # This is which coefficient in the original form the
                # current coefficient is.
                # Consider f*v*dx + g*v*ds, the full form contains two
                # coefficients, but each integral only requires one.
                coefficient_numbers.append(form_data.original_coefficient_positions[i])
        for i, coefficient in enumerate(coefficients):
            self.coefficient_args.append(
                self._coefficient(coefficient, "w_%d" % i))
        self.kernel.coefficient_numbers = tuple(coefficient_numbers)


class DenseSparsity(object):
    def __init__(self, rset, cset):
        self.shape = (1, 1)
        self._nrows = rset.size
        self._ncols = cset.size
        self._dims = (((1, 1), ), )
        self.dims = self._dims
        self.dsets = rset, cset

    def __getitem__(self, *args):
        return self


class MatArg(seq.Arg):
    def c_addto(self, i, j, buf_name, tmp_name, tmp_decl,
                extruded=None, is_facet=False, applied_blas=False):
        # Override global c_addto to index the map locally rather than globally.
        # Replaces MatSetValuesLocal with MatSetValues
        from pyop2.utils import as_tuple
        rmap, cmap = as_tuple(self.map, op2.Map)
        rset, cset = self.data.sparsity.dsets
        nrows = sum(m.arity*s.cdim for m, s in zip(rmap, rset))
        ncols = sum(m.arity*s.cdim for m, s in zip(cmap, cset))
        rows_str = "%s + n * %s" % (self.c_map_name(0, i), nrows)
        cols_str = "%s + n * %s" % (self.c_map_name(1, j), ncols)

        if extruded is not None:
            raise NotImplementedError("Not for extruded right now")

        if is_facet:
            raise NotImplementedError("Not for interior facets and extruded")

        ret = []
        addto_name = buf_name
        if rmap.vector_index is not None or cmap.vector_index is not None:
            raise NotImplementedError
        ret.append("""MatSetValues(%(mat)s, %(nrows)s, %(rows)s,
                                         %(ncols)s, %(cols)s,
                                         (const PetscScalar *)%(vals)s,
                                         %(insert)s);""" %
                   {'mat': self.c_arg_name(i, j),
                    'vals': addto_name,
                    'nrows': nrows,
                    'ncols': ncols,
                    'rows': rows_str,
                    'cols': cols_str,
                    'insert': "INSERT_VALUES" if self.access == op2.WRITE else "ADD_VALUES"})
        return "\n".join(ret)


class DenseMat(pyop2.Mat):
    def __init__(self, dset):
        self._sparsity = DenseSparsity(dset, dset)
        self.dtype = numpy.dtype(PETSc.ScalarType)

    def __call__(self, access, path):
        path_maps = [arg.map for arg in path]
        path_idxs = [arg.idx for arg in path]
        return MatArg(self, path_maps, path_idxs, access)


class DatArg(seq.Arg):

    def c_buffer_gather(self, size, idx, buf_name, extruded=False):
        dim = self.data.cdim
        val = ";\n".join(["%(name)s[i_0*%(dim)d%(ofs)s] = *(%(ind)s%(ofs)s);\n" %
                          {"name": buf_name,
                           "dim": dim,
                           "ind": self.c_kernel_arg(idx, extruded=extruded),
                           "ofs": " + %s" % j if j else ""} for j in range(dim)])
        val = val.replace("[i *", "[n *")
        return val

    def c_buffer_scatter_vec(self, count, i, j, mxofs, buf_name, extruded=False):
        dim = self.data.split[i].cdim
        ind = self.c_kernel_arg(count, i, j, extruded=extruded)
        ind = ind.replace("[i *", "[n *")
        map_val = "%(map_name)s[%(var)s * %(arity)s + %(idx)s]" % \
            {'name': self.c_arg_name(i),
             'map_name': self.c_map_name(i, 0),
             'var': "n",
             'arity': self.map.split[i].arity,
             'idx': "i_%d" % self.idx.index}

        val = "\n".join(["if (%(map_val)s >= 0) {*(%(ind)s%(nfofs)s) %(op)s %(name)s[i_0*%(dim)d%(nfofs)s%(mxofs)s];}" %
                         {"ind": ind,
                          "op": "=" if self.access == op2.WRITE else "+=",
                          "name": buf_name,
                          "dim": dim,
                          "nfofs": " + %d" % o if o else "",
                          "mxofs": " + %d" % (mxofs[0] * dim) if mxofs else "",
                          "map_val": map_val}
                         for o in range(dim)])
        return val


class DenseDat(pyop2.Dat):
    def __init__(self, dset):
        self._dataset = dset
        self.dtype = numpy.dtype(PETSc.ScalarType)
        self._soa = False

    def __call__(self, access, path):
        return DatArg(self, map=path.map, idx=path.idx, access=access)


class JITModule(seq.JITModule):
    @classmethod
    def _cache_key(cls, *args, **kwargs):
        # No caching
        return None


CompiledKernel = namedtuple('CompiledKernel', ["funptr", "kinfo"])


def matrix_funptr(form, state):
    from firedrake.tsfc_interface import compile_form
    test, trial = map(operator.methodcaller("function_space"), form.arguments())
    if test != trial:
        raise NotImplementedError("Only for matching test and trial spaces")

    if state is not None:
        interface = PatchKernelBuilder(state)
    else:
        interface = None

    kernels = compile_form(form, "subspace_form", split=False, interface=interface)

    cell_kernels = []
    int_facet_kernels = []
    for kernel in kernels:
        kinfo = kernel.kinfo

        if kinfo.subdomain_id != "otherwise":
            raise NotImplementedError("Only for full domain integrals")
        if kinfo.integral_type not in {"cell", "interior_facet"}:
            raise NotImplementedError("Only for cell or interior facet integrals")

        # OK, now we've validated the kernel, let's build the callback
        args = []

        if kinfo.integral_type == "cell":
            get_map = operator.methodcaller("cell_node_map")
            kernels = cell_kernels
        elif kinfo.integral_type == "interior_facet":
            get_map = operator.methodcaller("interior_facet_node_map")
            kernels = int_facet_kernels
        else:
            get_map = None

        toset = op2.Set(1, comm=test.comm)
        dofset = op2.DataSet(toset, 1)
        arity = sum(m.arity*s.cdim
                    for m, s in zip(get_map(test),
                                    test.dof_dset))
        iterset = get_map(test).iterset
        entity_node_map = op2.Map(iterset,
                                  toset, arity,
                                  values=numpy.zeros(iterset.total_size*arity, dtype=IntType))
        mat = DenseMat(dofset)

        arg = mat(op2.INC, (entity_node_map[op2.i[0]],
                            entity_node_map[op2.i[1]]))
        arg.position = 0
        args.append(arg)
        statedat = DenseDat(dofset)
        statearg = statedat(op2.READ, entity_node_map[op2.i[0]])

        mesh = form.ufl_domains()[kinfo.domain_number]
        arg = mesh.coordinates.dat(op2.READ, get_map(mesh.coordinates)[op2.i[0]])
        arg.position = 1
        args.append(arg)
        for n in kinfo.coefficient_map:
            c = form.coefficients()[n]
            if c is state:
                statearg.position = len(args)
                args.append(statearg)
                continue
            for (i, c_) in enumerate(c.split()):
                map_ = get_map(c_)
                if map_ is not None:
                    map_ = map_[op2.i[0]]
                arg = c_.dat(op2.READ, map_)
                arg.position = len(args)
                args.append(arg)

        if kinfo.integral_type == "interior_facet":
            arg = test.ufl_domain().interior_facets.local_facet_dat(op2.READ)
            arg.position = len(args)
            args.append(arg)
        iterset = op2.Subset(iterset, [0])
        mod = JITModule(kinfo.kernel, iterset, *args)
        kernels.append(CompiledKernel(mod._fun, kinfo))
    return cell_kernels, int_facet_kernels


def residual_funptr(form, state):
    from firedrake.tsfc_interface import compile_form
    test, = map(operator.methodcaller("function_space"), form.arguments())

    if state.function_space() != test:
        raise NotImplementedError("State and test space must be dual to one-another")

    if state is not None:
        interface = PatchKernelBuilder(state)
    else:
        interface = None

    kernel, = compile_form(form, "subspace_form", split=False, interface=interface)

    kinfo = kernel.kinfo

    if kinfo.subdomain_id != "otherwise":
        raise NotImplementedError("Only for full domain integrals")
    if kinfo.integral_type != "cell":
        raise NotImplementedError("Only for cell integrals")
    args = []

    toset = op2.Set(1, comm=test.comm)
    dofset = op2.DataSet(toset, 1)
    arity = sum(m.arity*s.cdim
                for m, s in zip(test.cell_node_map(),
                                test.dof_dset))
    iterset = test.cell_node_map().iterset
    cell_node_map = op2.Map(iterset,
                            toset, arity,
                            values=numpy.zeros(iterset.total_size*arity, dtype=IntType))
    dat = DenseDat(dofset)

    statedat = DenseDat(dofset)
    statearg = statedat(op2.READ, cell_node_map[op2.i[0]])

    arg = dat(op2.INC, cell_node_map[op2.i[0]])
    arg.position = 0
    args.append(arg)

    mesh = form.ufl_domains()[kinfo.domain_number]
    arg = mesh.coordinates.dat(op2.READ, mesh.coordinates.cell_node_map()[op2.i[0]])
    arg.position = 1
    args.append(arg)
    for n in kinfo.coefficient_map:
        c = form.coefficients()[n]
        if c is state:
            statearg.position = len(args)
            args.append(statearg)
            continue
        for (i, c_) in enumerate(c.split()):
            map_ = c_.cell_node_map()
            if map_ is not None:
                map_ = map_[op2.i[0]]
            arg = c_.dat(op2.READ, map_)
            arg.position = len(args)
            args.append(arg)

    iterset = op2.Subset(mesh.cell_set, [0])
    mod = JITModule(kinfo.kernel, iterset, *args)
    return mod._fun, kinfo


def bcdofs(bc, ghost=True):
    # Return the global dofs fixed by a DirichletBC
    # in the numbering given by concatenation of all the
    # subspaces of a mixed function space
    Z = bc.function_space()
    while Z.parent is not None:
        Z = Z.parent

    indices = bc._indices
    offset = 0

    for (i, idx) in enumerate(indices):
        if isinstance(Z.ufl_element(), VectorElement):
            offset += idx
            assert i == len(indices)-1  # assert we're at the end of the chain
            assert Z.sub(idx).value_size == 1
        elif isinstance(Z.ufl_element(), MixedElement):
            if ghost:
                offset += sum(Z.sub(j).dof_count for j in range(idx))
            else:
                offset += sum(Z.sub(j).dof_dset.size * Z.sub(j).value_size for j in range(idx))
        else:
            raise NotImplementedError("How are you taking a .sub?")

        Z = Z.sub(idx)

    if Z.parent is not None and isinstance(Z.parent.ufl_element(), VectorElement):
        bs = Z.parent.value_size
        start = 0
        stop = 1
    else:
        bs = Z.value_size
        start = 0
        stop = bs
    nodes = bc.nodes
    if not ghost:
        nodes = nodes[nodes < Z.dof_dset.size]

    return numpy.concatenate([nodes*bs + j for j in range(start, stop)]) + offset


def select_entity(p, dm=None, exclude=None):
    """Filter entities based on some label.

    :arg p: the entity.
    :arg dm: the DMPlex object to query for labels.
    :arg exclude: The label marking points to exclude."""
    if exclude is None:
        return True
    else:
        # If the exclude label marks this point (the value is not -1),
        # we don't want it.
        return dm.getLabelValue(exclude, p) == -1


class PlaneSmoother(object):
    @staticmethod
    def coords(dm, p):
        coordsSection = dm.getCoordinateSection()
        coordsDM = dm.getCoordinateDM()
        dim = coordsDM.getDimension()
        coordsVec = dm.getCoordinatesLocal()
        return dm.getVecClosure(coordsSection, coordsVec, p).reshape(-1, dim).mean(axis=0)

    def sort_entities(self, dm, axis, dir, ndiv):
        # compute
        # [(pStart, (x, y, z)), (pEnd, (x, y, z))]
        select = partial(select_entity, dm=dm, exclude="pyop2_ghost")
        entities = [(p, self.coords(dm, p)) for p in
                    filter(select, range(*dm.getChart()))]

        minx = min(entities, key=lambda z: z[1][axis])[1][axis]
        maxx = max(entities, key=lambda z: z[1][axis])[1][axis]

        def keyfunc(z):
            coords = tuple(z[1])
            return (coords[axis], ) + tuple(coords[:axis] + coords[axis+1:])

        s = sorted(entities, key=keyfunc, reverse=(dir == -1))

        divisions = numpy.linspace(minx, maxx, ndiv+1)
        (entities, coords) = zip(*s)
        coords = [c[axis] for c in coords]
        indices = numpy.searchsorted(coords[::dir], divisions)

        out = []
        for k in range(ndiv):
            out.append(entities[indices[k]:indices[k+1]])
        out.append(entities[indices[-1]:])

        return out

    def __call__(self, pc):
        dm = pc.getDM()
        prefix = pc.getOptionsPrefix()
        sentinel = object()
        sweeps = PETSc.Options(prefix).getString("pc_patch_construct_ps_sweeps", default=sentinel)
        if sweeps == sentinel:
            raise ValueError("Must set %spc_patch_construct_ps_sweeps" % prefix)

        patches = []
        for sweep in sweeps.split(':'):
            axis = int(sweep[0])
            dir = {'+': +1, '-': -1}[sweep[1]]
            ndiv = int(sweep[2:])

            entities = self.sort_entities(dm, axis, dir, ndiv)
            for patch in entities:
                iset = PETSc.IS().createGeneral(patch, comm=PETSc.COMM_SELF)
                patches.append(iset)

        iterationSet = PETSc.IS().createStride(size=len(patches), first=0, step=1, comm=PETSc.COMM_SELF)
        return (patches, iterationSet)


class PatchPC(PCBase):
    def initialize(self, pc):
        A, P = pc.getOperators()

        ctx = get_appctx(pc.getDM())
        if ctx is None:
            raise ValueError("No context found on form")
        if not isinstance(ctx, _SNESContext):
            raise ValueError("Don't know how to get form from %r", ctx)

        if P.getType() == "python":
            ictx = P.getPythonContext()
            if ictx is None:
                raise ValueError("No context found on matrix")
            if not isinstance(ictx, ImplicitMatrixContext):
                raise ValueError("Don't know how to get form from %r", ctx)
            J = ictx.a
            bcs = ictx.row_bcs
            if bcs != ictx.col_bcs:
                raise NotImplementedError("Row and column bcs must match")
        else:
            J = ctx.Jp or ctx.J
            bcs = ctx._problem.bcs

        mesh = J.ufl_domain()
        if mesh.cell_set._extruded:
            raise NotImplementedError("Not implemented on extruded meshes")

        if "overlap_type" not in mesh._distribution_parameters:
            if mesh.mpi_comm().size > 1:
                # Want to do
                # warnings.warn("You almost surely want to set an overlap_type in your mesh's distribution_parameters.")
                # but doesn't warn!
                PETSc.Sys.Print("Warning: you almost surely want to set an overlap_type in your mesh's distribution_parameters.")

        patch = PETSc.PC().create(comm=pc.comm)
        patch.setOptionsPrefix(pc.getOptionsPrefix() + "patch_")
        patch.setOperators(A, P)
        patch.setType("patch")
        cell_kernels, int_facet_kernels = matrix_funptr(J, None)
        V, _ = map(operator.methodcaller("function_space"), J.arguments())
        mesh = V.ufl_domain()

        if len(bcs) > 0:
            ghost_bc_nodes = numpy.unique(numpy.concatenate([bcdofs(bc, ghost=True)
                                                             for bc in bcs]))
            global_bc_nodes = numpy.unique(numpy.concatenate([bcdofs(bc, ghost=False)
                                                              for bc in bcs]))
        else:
            ghost_bc_nodes = numpy.empty(0, dtype=PETSc.IntType)
            global_bc_nodes = numpy.empty(0, dtype=PETSc.IntType)

        cell_kernel, = cell_kernels
        op_coeffs = [mesh.coordinates]
        for n in cell_kernel.kinfo.coefficient_map:
            op_coeffs.append(J.coefficients()[n])

        op_args = []
        for c in op_coeffs:
            for c_ in c.split():
                op_args.append(c_.dat._data.ctypes.data)
                c_map = c_.cell_node_map()
                if c_map is not None:
                    op_args.append(c_map._values.ctypes.data)

        def op(pc, point, vec, mat, cellIS, cell_dofmap, cell_dofmapWithAll):
            cells = cellIS.indices
            ncell = len(cells)
            dofs = cell_dofmap.ctypes.data
            cell_kernel.funptr(0, ncell, cells.ctypes.data, mat.handle,
                               dofs, dofs, *op_args)

        has_int_facet_kernel = False
        if len(int_facet_kernels) > 0:
            int_facet_kernel, = int_facet_kernels
            has_int_facet_kernel = True
            facet_op_coeffs = [mesh.coordinates]
            for n in int_facet_kernel.kinfo.coefficient_map:
                facet_op_coeffs.append(J.coefficients()[n])

            facet_op_args = []
            for c in facet_op_coeffs:
                for c_ in c.split():
                    facet_op_args.append(c_.dat._data.ctypes.data)
                    c_map = c_.interior_facet_node_map()
                    if c_map is not None:
                        facet_op_args.append(c_map._values.ctypes.data)
            facet_op_args.append(J.ufl_domain().interior_facets.local_facet_dat._data.ctypes.data)

            point2facetnumber = J.ufl_domain().interior_facets.point2facetnumber

            def facet_op(pc, point, vec, mat, facetIS, facet_dofmap, facet_dofmapWithAll):
                facets = numpy.asarray(list(map(point2facetnumber.__getitem__, facetIS.indices)),
                                       dtype=IntType)
                nfacet = len(facets)
                dofs = facet_dofmap.ctypes.data
                int_facet_kernel.funptr(0, nfacet, facets.ctypes.data, mat.handle,
                                        dofs, dofs, *facet_op_args)

        patch.setDM(mesh._plex)
        patch.setPatchCellNumbering(mesh._cell_numbering)

        offsets = numpy.append([0], numpy.cumsum([W.dof_count
                                                  for W in V])).astype(PETSc.IntType)
        patch.setPatchDiscretisationInfo([W.dm for W in V],
                                         numpy.array([W.value_size for
                                                      W in V], dtype=PETSc.IntType),
                                         [W.cell_node_list for W in V],
                                         offsets,
                                         ghost_bc_nodes,
                                         global_bc_nodes)
        patch.setPatchComputeOperator(op)
        if has_int_facet_kernel:
            patch.setPatchComputeOperatorInteriorFacets(facet_op)
        patch.setPatchConstructType(patch.PatchConstructType.PYTHON,
                                    operator=self.user_construction_op)
        patch.setAttr("ctx", ctx)
        patch.incrementTabLevel(1, parent=pc)
        patch.setFromOptions()
        patch.setUp()
        self.patch = patch

    @staticmethod
    def user_construction_op(pc, *args, **kwargs):
        prefix = pc.getOptionsPrefix()
        sentinel = object()
        usercode = PETSc.Options(prefix).getString("pc_patch_construct_python_type", default=sentinel)
        if usercode == sentinel:
            raise ValueError("Must set %spc_patch_construct_python_type" % prefix)

        (modname, funname) = usercode.rsplit('.', 1)
        mod = __import__(modname)
        fun = getattr(mod, funname)
        if isinstance(fun, type):
            fun = fun()
        return fun(pc, *args, **kwargs)

    def update(self, pc):
        self.patch.setUp()

    def apply(self, pc, x, y):
        self.patch.apply(x, y)

    def applyTranspose(self, pc, x, y):
        self.patch.applyTranspose(x, y)

    def view(self, pc, viewer=None):
        self.patch.view(viewer=viewer)


class PatchSNES(SNESBase):
    def initialize(self, snes):
        ctx = get_appctx(snes.getDM())
        if ctx is None:
            raise ValueError("No context found on form")
        if not isinstance(ctx, _SNESContext):
            raise ValueError("Don't know how to get form from %r", ctx)
        F = ctx.F
        state = ctx._problem.u

        pc = snes.ksp.pc
        A, P = pc.getOperators()
        if P.getType() == "python":
            ictx = P.getPythonContext()
            if ictx is None:
                raise ValueError("No context found on matrix")
            if not isinstance(ictx, ImplicitMatrixContext):
                raise ValueError("Don't know how to get form from %r", ictx)
            J = ictx.a
            bcs = ictx.row_bcs
            if bcs != ictx.col_bcs:
                raise NotImplementedError("Row and column bcs must match")
        else:
            J = ctx.Jp or ctx.J
            bcs = ctx._problem.bcs

        mesh = J.ufl_domain()
        self.plex = mesh._plex
        self.ctx = ctx
        if mesh.cell_set._extruded:
            raise NotImplementedError("Not implemented on extruded meshes")

        if "overlap_type" not in mesh._distribution_parameters:
            if mesh.mpi_comm().size > 1:
                # Want to do
                # warnings.warn("You almost surely want to set an overlap_type in your mesh's distribution_parameters.")
                # but doesn't warn!
                PETSc.Sys.Print("Warning: you almost surely want to set an overlap_type in your mesh's distribution_parameters.")

        patch = PETSc.SNES().create(comm=snes.comm)
        patch.setOptionsPrefix(snes.getOptionsPrefix() + "patch_")
        patch.setType("patch")

        V, _ = map(operator.methodcaller("function_space"), J.arguments())
        mesh = V.ufl_domain()

        if len(bcs) > 0:
            ghost_bc_nodes = numpy.unique(numpy.concatenate([bcdofs(bc, ghost=True)
                                                             for bc in bcs]))
            global_bc_nodes = numpy.unique(numpy.concatenate([bcdofs(bc, ghost=False)
                                                              for bc in bcs]))
        else:
            ghost_bc_nodes = numpy.empty(0, dtype=PETSc.IntType)
            global_bc_nodes = numpy.empty(0, dtype=PETSc.IntType)

        Jfunptr, Jkinfo = matrix_funptr(J, state)
        Jop_coeffs = [mesh.coordinates]
        for n in Jkinfo.coefficient_map:
            Jop_coeffs.append(J.coefficients()[n])

        Jop_args = []
        Jop_state_slot = None
        for c in Jop_coeffs:
            if c is state:
                Jop_state_slot = len(Jop_args)
                Jop_args.append(None)
                Jop_args.append(None)
                continue
            for c_ in c.split():
                Jop_args.append(c_.dat._data.ctypes.data)
                c_map = c_.cell_node_map()
                if c_map is not None:
                    Jop_args.append(c_map._values.ctypes.data)

        def Jop(pc, point, vec, mat, cellIS, cell_dofmap, cell_dofmapWithAll):
            cells = cellIS.indices
            ncell = len(cells)
            dofs = cell_dofmap.ctypes.data
            if cell_dofmapWithAll is not None:
                dofsWithAll = cell_dofmapWithAll.ctypes.data
            else:
                dofsWithAll = None
            mat.zeroEntries()
            if Jop_state_slot is not None:
                assert dofsWithAll is not None
                Jop_args[Jop_state_slot] = vec.array_r.ctypes.data
                Jop_args[Jop_state_slot + 1] = dofsWithAll
            Jfunptr(0, ncell, cells.ctypes.data, mat.handle,
                    dofs, dofs, *Jop_args)
            mat.assemble()

        Ffunptr, Fkinfo = residual_funptr(F, state)
        Fop_coeffs = [mesh.coordinates]
        for n in Fkinfo.coefficient_map:
            Fop_coeffs.append(F.coefficients()[n])
        assert any(c is state for c in Fop_coeffs), "Couldn't find state vector in F.coefficients()"

        Fop_args = []
        Fop_state_slot = None
        for c in Fop_coeffs:
            if c is state:
                Fop_state_slot = len(Fop_args)
                Fop_args.append(None)
                Fop_args.append(None)
                continue
            for c_ in c.split():
                Fop_args.append(c_.dat._data.ctypes.data)
                c_map = c_.cell_node_map()
                if c_map is not None:
                    Fop_args.append(c_map._values.ctypes.data)

        assert Fop_state_slot is not None

        def Fop(pc, point, vec, out, cellIS, cell_dofmap, cell_dofmapWithAll):
            cells = cellIS.indices
            ncell = len(cells)
            dofs = cell_dofmap.ctypes.data
            dofsWithAll = cell_dofmapWithAll.ctypes.data
            out.set(0)
            outdata = out.array
            Fop_args[Fop_state_slot] = vec.array_r.ctypes.data
            Fop_args[Fop_state_slot + 1] = dofsWithAll
            Ffunptr(0, ncell, cells.ctypes.data, outdata.ctypes.data,
                    dofs, *Fop_args)
            # FIXME: Do we need this, I think not.
            out.assemble()

        patch.setDM(mesh._plex)
        patch.setPatchCellNumbering(mesh._cell_numbering)

        offsets = numpy.append([0], numpy.cumsum([W.dof_count
                                                  for W in V])).astype(PETSc.IntType)
        patch.setPatchDiscretisationInfo([W.dm for W in V],
                                         numpy.array([W.value_size for
                                                      W in V], dtype=PETSc.IntType),
                                         [W.cell_node_list for W in V],
                                         offsets,
                                         ghost_bc_nodes,
                                         global_bc_nodes)
        patch.setPatchComputeOperator(Jop)
        patch.setPatchComputeFunction(Fop)
        patch.setPatchConstructType(PETSc.PC.PatchConstructType.PYTHON,
                                    operator=self.user_construction_op)

        (f, residual) = snes.getFunction()
        assert residual is not None
        (fun, args, kargs) = residual
        patch.setFunction(fun, f.duplicate(), args=args, kargs=kargs)

        patch.setAttr("ctx", ctx)
        patch.incrementTabLevel(1, parent=snes)
        patch.setTolerances(max_it=1)
        patch.setConvergenceTest("skip")
        patch.setFromOptions()
        patch.setUp()
        self.patch = patch

        # Need an empty RHS for the solve,
        # PCApply can't deal with RHS = NULL
        self.dummy = f.duplicate()

    @staticmethod
    def user_construction_op(pc, *args, **kwargs):
        prefix = pc.getOptionsPrefix()
        sentinel = object()
        usercode = PETSc.Options(prefix).getString("snes_patch_construct_python_type", default=sentinel)
        if usercode == sentinel:
            raise ValueError("Must set %ssnes_patch_construct_python_type" % prefix)

        (modname, funname) = usercode.rsplit('.', 1)
        mod = __import__(modname)
        fun = getattr(mod, funname)
        if isinstance(fun, type):
            fun = fun()
        return fun(pc, *args, **kwargs)

    def update(self, pc):
        self.patch.setUp()

    def step(self, snes, x, f, y):
        push_appctx(self.plex, self.ctx)
        x.copy(y)
        self.patch.solve(snes.vec_rhs or self.dummy, y)
        y.axpy(-1, x)
        y.scale(-1)
        snes.setConvergedReason(self.patch.getConvergedReason())
        pop_appctx(self.plex)

    def view(self, pc, viewer=None):
        self.patch.view(viewer=viewer)
