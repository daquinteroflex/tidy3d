"""Solve for modes in a 2D cross-sectional plane in a simulation, assuming translational
invariance along a given propagation axis.
"""

from __future__ import annotations

from functools import wraps
from math import isclose
from typing import Dict, List, Tuple, Union

import numpy as np
import pydantic.v1 as pydantic
import xarray as xr
from matplotlib.collections import PatchCollection
from matplotlib.patches import Rectangle

from ...components.base import Tidy3dBaseModel, cached_property, skip_if_fields_missing
from ...components.boundary import PML, Absorber, Boundary, BoundarySpec, PECBoundary, StablePML
from ...components.data.data_array import (
    FreqModeDataArray,
    ModeIndexDataArray,
    ScalarModeFieldDataArray,
)
from ...components.data.monitor_data import ModeSolverData
from ...components.data.sim_data import SimulationData
from ...components.eme.data.sim_data import EMESimulationData
from ...components.eme.simulation import EMESimulation
from ...components.geometry.base import Box
from ...components.grid.grid import Grid
from ...components.medium import FullyAnisotropicMedium
from ...components.mode import ModeSpec
from ...components.monitor import ModeMonitor, ModeSolverMonitor
from ...components.simulation import Simulation
from ...components.source import ModeSource, SourceTime
from ...components.types import (
    TYPE_TAG_STR,
    ArrayComplex3D,
    ArrayComplex4D,
    ArrayFloat1D,
    Ax,
    Axis,
    Direction,
    EpsSpecType,
    FreqArray,
    Literal,
    PlotScale,
    Symmetry,
)
from ...components.validators import validate_freqs_min, validate_freqs_not_empty
from ...components.viz import plot_params_pml
from ...constants import C_0
from ...exceptions import SetupError, ValidationError
from ...log import log

# Importing the local solver may not work if e.g. scipy is not installed
IMPORT_ERROR_MSG = """Could not import local solver, 'ModeSolver' objects can still be constructed
but will have to be run through the server.
"""
try:
    from .solver import compute_modes

    LOCAL_SOLVER_IMPORTED = True
except ImportError:
    log.warning(IMPORT_ERROR_MSG)
    LOCAL_SOLVER_IMPORTED = False

FIELD = Tuple[ArrayComplex3D, ArrayComplex3D, ArrayComplex3D]
MODE_MONITOR_NAME = "<<<MODE_SOLVER_MONITOR>>>"

# Warning for field intensity at edges over total field intensity larger than this value
FIELD_DECAY_CUTOFF = 1e-2

# Maximum allowed size of the field data produced by the mode solver
MAX_MODES_DATA_SIZE_GB = 20

MODE_SIMULATION_TYPE = Union[Simulation, EMESimulation]
MODE_SIMULATION_DATA_TYPE = Union[SimulationData, EMESimulationData]
MODE_PLANE_TYPE = Union[Box, ModeSource, ModeMonitor, ModeSolverMonitor]


def require_fdtd_simulation(fn):
    """Decorate a function to check that ``simulation`` is an FDTD ``Simulation``."""

    @wraps(fn)
    def _fn(self, **kwargs):
        """New decorated function."""
        if not isinstance(self.simulation, Simulation):
            raise SetupError(
                f"The function '{fn.__name__}' is only supported "
                "for 'simulation' of type FDTD 'Simulation'."
            )
        return fn(self, **kwargs)

    return _fn


class ModeSolver(Tidy3dBaseModel):
    """
    Interface for solving electromagnetic eigenmodes in a 2D plane with translational
    invariance in the third dimension.

    See Also
    --------

    :class:`ModeSource`:
        Injects current source to excite modal profile on finite extent plane.

    **Notebooks:**
        * `Waveguide Y junction <../../notebooks/YJunction.html>`_
        * `Photonic crystal waveguide polarization filter <../../../notebooks/PhotonicCrystalWaveguidePolarizationFilter.html>`_

    **Lectures:**
        * `Prelude to Integrated Photonics Simulation: Mode Injection <https://www.flexcompute.com/fdtd101/Lecture-4-Prelude-to-Integrated-Photonics-Simulation-Mode-Injection/>`_
    """

    simulation: MODE_SIMULATION_TYPE = pydantic.Field(
        ...,
        title="Simulation",
        description="Simulation or EMESimulation defining all structures and mediums.",
        discriminator="type",
    )

    plane: MODE_PLANE_TYPE = pydantic.Field(
        ...,
        title="Plane",
        description="Cross-sectional plane in which the mode will be computed.",
        discriminator=TYPE_TAG_STR,
    )

    mode_spec: ModeSpec = pydantic.Field(
        ...,
        title="Mode specification",
        description="Container with specifications about the modes to be solved for.",
    )

    freqs: FreqArray = pydantic.Field(
        ..., title="Frequencies", description="A list of frequencies at which to solve."
    )

    direction: Direction = pydantic.Field(
        "+",
        title="Propagation direction",
        description="Direction of waveguide mode propagation along the axis defined by its normal "
        "dimension.",
    )

    colocate: bool = pydantic.Field(
        True,
        title="Colocate fields",
        description="Toggle whether fields should be colocated to grid cell boundaries (i.e. "
        "primal grid nodes). Default is ``True``.",
    )

    @pydantic.validator("plane", always=True)
    def is_plane(cls, val):
        """Raise validation error if not planar."""
        if val.size.count(0.0) != 1:
            raise ValidationError(f"ModeSolver plane must be planar, given size={val}")
        return val

    _freqs_not_empty = validate_freqs_not_empty()
    _freqs_lower_bound = validate_freqs_min()

    @pydantic.validator("plane", always=True)
    @skip_if_fields_missing(["simulation"])
    def plane_in_sim_bounds(cls, val, values):
        """Check that the plane is at least partially inside the simulation bounds."""
        sim_center = values.get("simulation").center
        sim_size = values.get("simulation").size
        sim_box = Box(size=sim_size, center=sim_center)

        if not sim_box.intersects(val):
            raise SetupError("'ModeSolver.plane' must intersect 'ModeSolver.simulation'.")
        return val

    @cached_property
    def normal_axis(self) -> Axis:
        """Axis normal to the mode plane."""
        return self.plane.size.index(0.0)

    @cached_property
    def solver_symmetry(self) -> Tuple[Symmetry, Symmetry]:
        """Get symmetry for solver for propagation along self.normal axis."""
        mode_symmetry = list(self.simulation.symmetry)
        for dim in range(3):
            if self.simulation.center[dim] != self.plane.center[dim]:
                mode_symmetry[dim] = 0
        _, solver_sym = self.plane.pop_axis(mode_symmetry, axis=self.normal_axis)
        return solver_sym

    def _get_solver_grid(
        self, keep_additional_layers: bool = False, truncate_symmetry: bool = True
    ) -> Grid:
        """Grid for the mode solver, not snapped to plane or simulation zero dims, and optionally
        corrected for symmetries.

        Parameters
        ----------
        keep_additional_layers : bool = False
            Do not discard layers of cells in front and behind the main layer of cells. Together they
            represent the region where custom medium data is needed for proper subpixel.
        truncate_symmetry : bool = True
            Truncate to symmetry quadrant if symmetry present.

        Returns
        -------
        :class:.`Grid`
            The resulting grid.
        """

        monitor = self.to_mode_solver_monitor(name=MODE_MONITOR_NAME, colocate=False)

        span_inds = self.simulation._discretize_inds_monitor(monitor)

        # Remove extension along monitor normal
        if not keep_additional_layers:
            span_inds[self.normal_axis, 0] += 1
            span_inds[self.normal_axis, 1] -= 1

        # Do not extend if simulation has a single pixel along a dimension
        for dim, num_cells in enumerate(self.simulation.grid.num_cells):
            if num_cells <= 1:
                span_inds[dim] = [0, 1]

        # Truncate to symmetry quadrant if symmetry present
        if truncate_symmetry:
            _, plane_inds = Box.pop_axis([0, 1, 2], self.normal_axis)
            for dim, sym in enumerate(self.solver_symmetry):
                if sym != 0:
                    span_inds[plane_inds[dim], 0] += np.diff(span_inds[plane_inds[dim]]) // 2

        return self.simulation._subgrid(span_inds=span_inds)

    @cached_property
    def _solver_grid(self) -> Grid:
        """Grid for the mode solver, not snapped to plane or simulation zero dims, and also with
        a small correction for symmetries. We don't do the snapping yet because 0-sized cells are
        currently confusing to the subpixel averaging. The final data coordinates along the
        plane normal dimension and dimensions where the simulation domain is 2D will be correctly
        set after the solve."""

        return self._get_solver_grid(keep_additional_layers=False, truncate_symmetry=True)

    @cached_property
    def _num_cells_freqs_modes(self) -> Tuple[int, int, int]:
        """Get the number of spatial points, number of freqs, and number of modes requested."""
        num_cells = np.prod(self._solver_grid.num_cells)
        num_modes = self.mode_spec.num_modes
        num_freqs = len(self.freqs)
        return num_cells, num_freqs, num_modes

    def solve(self) -> ModeSolverData:
        """:class:`.ModeSolverData` containing the field and effective index data.

        Returns
        -------
        ModeSolverData
            :class:`.ModeSolverData` object containing the effective index and mode fields.
        """
        log.warning(
            "Use the remote mode solver with subpixel averaging for better accuracy through "
            "'tidy3d.plugins.mode.web.run(...)'.",
            log_once=True,
        )
        return self.data

    def _freqs_for_group_index(self) -> FreqArray:
        """Get frequencies used to compute group index."""
        f_step = self.mode_spec.group_index_step
        fractional_steps = (1 - f_step, 1, 1 + f_step)
        return np.outer(self.freqs, fractional_steps).flatten()

    def _remove_freqs_for_group_index(self) -> FreqArray:
        """Remove frequencies used to compute group index.

        Returns
        -------
        FreqArray
            Filtered frequency array with only original values.
        """
        return np.array(self.freqs[1 : len(self.freqs) : 3])

    def _get_data_with_group_index(self) -> ModeSolverData:
        """:class:`.ModeSolverData` with fields, effective and group indices on unexpanded grid.

        Returns
        -------
        ModeSolverData
            :class:`.ModeSolverData` object containing the effective and group indices, and mode
            fields.
        """

        # create a copy with the required frequencies for numerical differentiation
        mode_spec = self.mode_spec.copy(update={"group_index_step": False})
        mode_solver = self.copy(
            update={"freqs": self._freqs_for_group_index(), "mode_spec": mode_spec}
        )

        return mode_solver.data_raw._group_index_post_process(self.mode_spec.group_index_step)

    @cached_property
    def grid_snapped(self) -> Grid:
        """The solver grid snapped to the plane normal and to simulation 0-sized dims if any."""
        grid_snapped = self._solver_grid.snap_to_box_zero_dim(self.plane)
        return self.simulation._snap_zero_dim(grid_snapped)

    @cached_property
    def data_raw(self) -> ModeSolverData:
        """:class:`.ModeSolverData` containing the field and effective index on unexpanded grid.

        Returns
        -------
        ModeSolverData
            :class:`.ModeSolverData` object containing the effective index and mode fields.
        """

        if self.mode_spec.group_index_step > 0:
            return self._get_data_with_group_index()

        # Compute data on the Yee grid
        mode_solver_data = self._data_on_yee_grid()

        # Colocate to grid boundaries if requested
        if self.colocate:
            mode_solver_data = self._colocate_data(mode_solver_data=mode_solver_data)

        # normalize modes
        self._normalize_modes(mode_solver_data=mode_solver_data)

        # filter polarization if requested
        if self.mode_spec.filter_pol is not None:
            self._filter_polarization(mode_solver_data=mode_solver_data)

        # sort modes if requested
        if self.mode_spec.track_freq and len(self.freqs) > 1:
            mode_solver_data = mode_solver_data.overlap_sort(self.mode_spec.track_freq)

        self._field_decay_warning(mode_solver_data.symmetry_expanded)

        return mode_solver_data

    def _data_on_yee_grid(self) -> ModeSolverData:
        """Solve for all modes, and construct data with fields on the Yee grid."""
        solver = self.reduced_simulation_copy

        _, _solver_coords = solver.plane.pop_axis(
            solver._solver_grid.boundaries.to_list, axis=solver.normal_axis
        )

        # Compute and store the modes at all frequencies
        n_complex, fields, eps_spec = solver._solve_all_freqs(
            coords=_solver_coords, symmetry=solver.solver_symmetry
        )

        # start a dictionary storing the data arrays for the ModeSolverData
        index_data = ModeIndexDataArray(
            np.stack(n_complex, axis=0),
            coords=dict(
                f=list(solver.freqs),
                mode_index=np.arange(solver.mode_spec.num_modes),
            ),
        )
        data_dict = {"n_complex": index_data}

        # Construct the field data on Yee grid
        for field_name in ("Ex", "Ey", "Ez", "Hx", "Hy", "Hz"):
            xyz_coords = solver.grid_snapped[field_name].to_list
            scalar_field_data = ScalarModeFieldDataArray(
                np.stack([field_freq[field_name] for field_freq in fields], axis=-2),
                coords=dict(
                    x=xyz_coords[0],
                    y=xyz_coords[1],
                    z=xyz_coords[2],
                    f=list(solver.freqs),
                    mode_index=np.arange(solver.mode_spec.num_modes),
                ),
            )
            data_dict[field_name] = scalar_field_data

        # finite grid corrections
        grid_factors = solver._grid_correction(
            simulation=solver.simulation,
            plane=solver.plane,
            mode_spec=solver.mode_spec,
            n_complex=index_data,
            direction=solver.direction,
        )

        # make mode solver data on the Yee grid
        mode_solver_monitor = solver.to_mode_solver_monitor(name=MODE_MONITOR_NAME, colocate=False)
        grid_expanded = solver.simulation.discretize_monitor(mode_solver_monitor)
        mode_solver_data = ModeSolverData(
            monitor=mode_solver_monitor,
            symmetry=solver.simulation.symmetry,
            symmetry_center=solver.simulation.center,
            grid_expanded=grid_expanded,
            grid_primal_correction=grid_factors[0],
            grid_dual_correction=grid_factors[1],
            eps_spec=eps_spec,
            **data_dict,
        )

        return mode_solver_data

    def _data_on_yee_grid_relative(self, basis: ModeSolverData) -> ModeSolverData:
        """Solve for all modes, and construct data with fields on the Yee grid."""
        if basis.monitor.colocate:
            raise ValidationError("Relative mode solver 'basis' must have 'colocate=False'.")
        _, _solver_coords = self.plane.pop_axis(
            self._solver_grid.boundaries.to_list, axis=self.normal_axis
        )

        basis_fields = []
        for freq_ind in range(len(basis.n_complex.f)):
            basis_fields_freq = {}
            for field_name in ("Ex", "Ey", "Ez", "Hx", "Hy", "Hz"):
                basis_fields_freq[field_name] = (
                    basis.field_components[field_name].isel(f=freq_ind).to_numpy()
                )
            basis_fields.append(basis_fields_freq)

        # Compute and store the modes at all frequencies
        n_complex, fields, eps_spec = self._solve_all_freqs_relative(
            coords=_solver_coords, symmetry=self.solver_symmetry, basis_fields=basis_fields
        )

        # start a dictionary storing the data arrays for the ModeSolverData
        index_data = ModeIndexDataArray(
            np.stack(n_complex, axis=0),
            coords=dict(
                f=list(self.freqs),
                mode_index=np.arange(self.mode_spec.num_modes),
            ),
        )
        data_dict = {"n_complex": index_data}

        # Construct the field data on Yee grid
        for field_name in ("Ex", "Ey", "Ez", "Hx", "Hy", "Hz"):
            xyz_coords = self.grid_snapped[field_name].to_list
            scalar_field_data = ScalarModeFieldDataArray(
                np.stack([field_freq[field_name] for field_freq in fields], axis=-2),
                coords=dict(
                    x=xyz_coords[0],
                    y=xyz_coords[1],
                    z=xyz_coords[2],
                    f=list(self.freqs),
                    mode_index=np.arange(self.mode_spec.num_modes),
                ),
            )
            data_dict[field_name] = scalar_field_data

        # finite grid corrections
        grid_factors = self._grid_correction(
            simulation=self.simulation,
            plane=self.plane,
            mode_spec=self.mode_spec,
            n_complex=index_data,
            direction=self.direction,
        )

        # make mode solver data on the Yee grid
        mode_solver_monitor = self.to_mode_solver_monitor(name=MODE_MONITOR_NAME, colocate=False)
        grid_expanded = self.simulation.discretize_monitor(mode_solver_monitor)
        mode_solver_data = ModeSolverData(
            monitor=mode_solver_monitor,
            symmetry=self.simulation.symmetry,
            symmetry_center=self.simulation.center,
            grid_expanded=grid_expanded,
            grid_primal_correction=grid_factors[0],
            grid_dual_correction=grid_factors[1],
            eps_spec=eps_spec,
            **data_dict,
        )

        return mode_solver_data

    def _colocate_data(self, mode_solver_data: ModeSolverData) -> ModeSolverData:
        """Colocate data to Yee grid boundaries."""

        # Get colocation coordinates in the solver plane
        _, plane_dims = self.plane.pop_axis("xyz", self.normal_axis)
        colocate_coords = {}
        for dim, sym in zip(plane_dims, self.solver_symmetry):
            coords = self.grid_snapped.boundaries.to_dict[dim]
            if len(coords) > 2:
                if sym == 0:
                    colocate_coords[dim] = coords[1:-1]
                else:
                    colocate_coords[dim] = coords[:-1]

        # Colocate input data to new coordinates
        data_dict_colocated = {}
        for key, field in mode_solver_data.symmetry_expanded.field_components.items():
            data_dict_colocated[key] = field.interp(**colocate_coords).astype(field.dtype)

        # Update data
        mode_solver_monitor = self.to_mode_solver_monitor(name=MODE_MONITOR_NAME)
        grid_expanded = self.simulation.discretize_monitor(mode_solver_monitor)
        data_dict_colocated.update({"monitor": mode_solver_monitor, "grid_expanded": grid_expanded})
        mode_solver_data = mode_solver_data._updated(update=data_dict_colocated)

        return mode_solver_data

    def _normalize_modes(self, mode_solver_data: ModeSolverData):
        """Normalize modes. Note: this modifies ``mode_solver_data`` in-place."""
        scaling = np.sqrt(np.abs(mode_solver_data.flux))
        for field in mode_solver_data.field_components.values():
            field /= scaling

    def _filter_polarization(self, mode_solver_data: ModeSolverData):
        """Filter polarization. Note: this modifies ``mode_solver_data`` in-place."""
        pol_frac = mode_solver_data.pol_fraction
        for ifreq in range(len(self.freqs)):
            te_frac = pol_frac.te.isel(f=ifreq)
            if self.mode_spec.filter_pol == "te":
                sort_inds = np.concatenate(
                    (
                        np.where(te_frac >= 0.5)[0],
                        np.where(te_frac < 0.5)[0],
                        np.where(np.isnan(te_frac))[0],
                    )
                )
            elif self.mode_spec.filter_pol == "tm":
                sort_inds = np.concatenate(
                    (
                        np.where(te_frac <= 0.5)[0],
                        np.where(te_frac > 0.5)[0],
                        np.where(np.isnan(te_frac))[0],
                    )
                )
            for data in list(mode_solver_data.field_components.values()) + [
                mode_solver_data.n_complex,
                mode_solver_data.grid_primal_correction,
                mode_solver_data.grid_dual_correction,
            ]:
                data.values[..., ifreq, :] = data.values[..., ifreq, sort_inds]

    @cached_property
    def data(self) -> ModeSolverData:
        """:class:`.ModeSolverData` containing the field and effective index data.

        Returns
        -------
        ModeSolverData
            :class:`.ModeSolverData` object containing the effective index and mode fields.
        """
        mode_solver_data = self.data_raw
        return mode_solver_data.symmetry_expanded_copy

    @cached_property
    def sim_data(self) -> MODE_SIMULATION_DATA_TYPE:
        """:class:`.SimulationData` object containing the :class:`.ModeSolverData` for this object.

        Returns
        -------
        SimulationData
            :class:`.SimulationData` object containing the effective index and mode fields.
        """
        monitor_data = self.data
        new_monitors = list(self.simulation.monitors) + [monitor_data.monitor]
        new_simulation = self.simulation.copy(update=dict(monitors=new_monitors))
        if isinstance(new_simulation, Simulation):
            return SimulationData(simulation=new_simulation, data=(monitor_data,))
        elif isinstance(new_simulation, EMESimulation):
            return EMESimulationData(
                simulation=new_simulation, data=(monitor_data,), smatrix=None, port_modes=None
            )
        else:
            raise SetupError(
                "The 'simulation' provided does not correspond to any known "
                "'AbstractSimulationData' type."
            )

    def _get_epsilon(self, freq: float) -> ArrayComplex4D:
        """Compute the epsilon tensor in the plane. Order of components is xx, xy, xz, yx, etc."""
        eps_keys = ["Ex", "Exy", "Exz", "Eyx", "Ey", "Eyz", "Ezx", "Ezy", "Ez"]
        eps_tensor = [
            self.simulation.epsilon_on_grid(self._solver_grid, key, freq) for key in eps_keys
        ]
        return np.stack(eps_tensor, axis=0)

    def _tensorial_material_profile_modal_plane_tranform(
        self, mat_data: ArrayComplex4D
    ) -> ArrayComplex4D:
        """For tensorial material response function such as epsilon and mu, pick and tranform it to
        modal plane with normal axis rotated to z.
        """
        # get rid of normal axis
        mat_tensor = np.take(mat_data, indices=[0], axis=1 + self.normal_axis)
        mat_tensor = np.squeeze(mat_tensor, axis=1 + self.normal_axis)

        # convert to into 3-by-3 representation for easier axis swap
        flat_shape = np.shape(mat_tensor)  # 9 components flat
        tensor_shape = [3, 3] + list(flat_shape[1:])  # 3-by-3 matrix
        mat_tensor = mat_tensor.reshape(tensor_shape)

        # swap axes to plane coordinates (normal_axis goes to z)
        if self.normal_axis == 0:
            # swap x and y
            mat_tensor[[0, 1], :, ...] = mat_tensor[[1, 0], :, ...]
            mat_tensor[:, [0, 1], ...] = mat_tensor[:, [1, 0], ...]
        if self.normal_axis <= 1:
            # swap x (normal_axis==0) or y (normal_axis==1) and z
            mat_tensor[[1, 2], :, ...] = mat_tensor[[2, 1], :, ...]
            mat_tensor[:, [1, 2], ...] = mat_tensor[:, [2, 1], ...]

        # back to "flat" representation
        mat_tensor = mat_tensor.reshape(flat_shape)

        # construct to feed to mode solver
        return mat_tensor

    def _diagonal_material_profile_modal_plane_tranform(
        self, mat_data: ArrayComplex4D
    ) -> ArrayComplex3D:
        """For diagonal material response function such as epsilon and mu, pick and tranform it to
        modal plane with normal axis rotated to z.
        """
        # get rid of normal axis
        mat_tensor = np.take(mat_data, indices=[0], axis=1 + self.normal_axis)
        mat_tensor = np.squeeze(mat_tensor, axis=1 + self.normal_axis)

        # swap axes to plane coordinates (normal_axis goes to z)
        if self.normal_axis == 0:
            # swap x and y
            mat_tensor[[0, 1], :, ...] = mat_tensor[[1, 0], :, ...]
        if self.normal_axis <= 1:
            # swap x (normal_axis==0) or y (normal_axis==1) and z
            mat_tensor[[1, 2], :, ...] = mat_tensor[[2, 1], :, ...]

        # construct to feed to mode solver
        return mat_tensor

    def _solver_eps(self, freq: float) -> ArrayComplex4D:
        """Diagonal permittivity in the shape needed by solver, with normal axis rotated to z."""

        # Get diagonal epsilon components in the plane
        eps_tensor = self._get_epsilon(freq)
        # tranformation
        return self._tensorial_material_profile_modal_plane_tranform(eps_tensor)

    def _solve_all_freqs(
        self,
        coords: Tuple[ArrayFloat1D, ArrayFloat1D],
        symmetry: Tuple[Symmetry, Symmetry],
    ) -> Tuple[List[float], List[Dict[str, ArrayComplex4D]], List[EpsSpecType]]:
        """Call the mode solver at all requested frequencies."""

        fields = []
        n_complex = []
        eps_spec = []
        for freq in self.freqs:
            n_freq, fields_freq, eps_spec_freq = self._solve_single_freq(
                freq=freq, coords=coords, symmetry=symmetry
            )
            fields.append(fields_freq)
            n_complex.append(n_freq)
            eps_spec.append(eps_spec_freq)
        return n_complex, fields, eps_spec

    def _solve_all_freqs_relative(
        self,
        coords: Tuple[ArrayFloat1D, ArrayFloat1D],
        symmetry: Tuple[Symmetry, Symmetry],
        basis_fields: List[Dict[str, ArrayComplex4D]],
    ) -> Tuple[List[float], List[Dict[str, ArrayComplex4D]], List[EpsSpecType]]:
        """Call the mode solver at all requested frequencies."""

        fields = []
        n_complex = []
        eps_spec = []
        for freq, basis_fields_freq in zip(self.freqs, basis_fields):
            n_freq, fields_freq, eps_spec_freq = self._solve_single_freq_relative(
                freq=freq, coords=coords, symmetry=symmetry, basis_fields=basis_fields_freq
            )
            fields.append(fields_freq)
            n_complex.append(n_freq)
            eps_spec.append(eps_spec_freq)

        return n_complex, fields, eps_spec

    def _postprocess_solver_fields(self, solver_fields):
        """Postprocess `solver_fields` from `compute_modes` to proper coordinate"""
        fields = {key: [] for key in ("Ex", "Ey", "Ez", "Hx", "Hy", "Hz")}
        for mode_index in range(self.mode_spec.num_modes):
            # Get E and H fields at the current mode_index
            ((Ex, Ey, Ez), (Hx, Hy, Hz)) = self._process_fields(solver_fields, mode_index)

            # Note: back in original coordinates
            fields_mode = {"Ex": Ex, "Ey": Ey, "Ez": Ez, "Hx": Hx, "Hy": Hy, "Hz": Hz}
            for field_name, field in fields_mode.items():
                fields[field_name].append(field)

        for field_name, field in fields.items():
            fields[field_name] = np.stack(field, axis=-1)
        return fields

    def _solve_single_freq(
        self,
        freq: float,
        coords: Tuple[ArrayFloat1D, ArrayFloat1D],
        symmetry: Tuple[Symmetry, Symmetry],
    ) -> Tuple[float, Dict[str, ArrayComplex4D], EpsSpecType]:
        """Call the mode solver at a single frequency.

        The fields are rotated from propagation coordinates back to global coordinates.
        """

        if not LOCAL_SOLVER_IMPORTED:
            raise ImportError(IMPORT_ERROR_MSG)

        solver_fields, n_complex, eps_spec = compute_modes(
            eps_cross=self._solver_eps(freq),
            coords=coords,
            freq=freq,
            mode_spec=self.mode_spec,
            symmetry=symmetry,
            direction=self.direction,
        )

        fields = self._postprocess_solver_fields(solver_fields)
        return n_complex, fields, eps_spec

    def _rotate_field_coords_inverse(self, field: FIELD) -> FIELD:
        """Move the propagation axis to the z axis in the array."""
        f_x, f_y, f_z = np.moveaxis(field, source=1 + self.normal_axis, destination=3)
        f_n, f_ts = self.plane.pop_axis((f_x, f_y, f_z), axis=self.normal_axis)
        return np.stack(self.plane.unpop_axis(f_n, f_ts, axis=2), axis=0)

    def _postprocess_solver_fields_inverse(self, fields):
        """Convert ``fields`` to ``solver_fields``. Doesn't change gauge."""
        E = [fields[key] for key in ("Ex", "Ey", "Ez")]
        H = [fields[key] for key in ("Hx", "Hy", "Hz")]

        (Ex, Ey, Ez) = self._rotate_field_coords_inverse(E)
        (Hx, Hy, Hz) = self._rotate_field_coords_inverse(H)

        # apply -1 to H fields if a reflection was involved in the rotation
        if self.normal_axis == 1:
            Hx *= -1
            Hy *= -1
            Hz *= -1

        solver_fields = np.stack((Ex, Ey, Ez, Hx, Hy, Hz), axis=0)
        return solver_fields

    def _solve_single_freq_relative(
        self,
        freq: float,
        coords: Tuple[ArrayFloat1D, ArrayFloat1D],
        symmetry: Tuple[Symmetry, Symmetry],
        basis_fields: Dict[str, ArrayComplex4D],
    ) -> Tuple[float, Dict[str, ArrayComplex4D], EpsSpecType]:
        """Call the mode solver at a single frequency.
        Modes are computed as linear combinations of ``basis_fields``.
        """

        if not LOCAL_SOLVER_IMPORTED:
            raise ImportError(IMPORT_ERROR_MSG)

        solver_basis_fields = self._postprocess_solver_fields_inverse(basis_fields)

        solver_fields, n_complex, eps_spec = compute_modes(
            eps_cross=self._solver_eps(freq),
            coords=coords,
            freq=freq,
            mode_spec=self.mode_spec,
            symmetry=symmetry,
            direction=self.direction,
            solver_basis_fields=solver_basis_fields,
        )

        fields = self._postprocess_solver_fields(solver_fields)
        return n_complex, fields, eps_spec

    def _rotate_field_coords(self, field: FIELD) -> FIELD:
        """Move the propagation axis=z to the proper order in the array."""
        f_x, f_y, f_z = np.moveaxis(field, source=3, destination=1 + self.normal_axis)
        return np.stack(self.plane.unpop_axis(f_z, (f_x, f_y), axis=self.normal_axis), axis=0)

    def _process_fields(
        self, mode_fields: ArrayComplex4D, mode_index: pydantic.NonNegativeInt
    ) -> Tuple[FIELD, FIELD]:
        """Transform solver fields to simulation axes and set gauge."""

        # Separate E and H fields (in solver coordinates)
        E, H = mode_fields[..., mode_index]

        # Set gauge to highest-amplitude in-plane E being real and positive
        ind_max = np.argmax(np.abs(E[:2]))
        phi = np.angle(E[:2].ravel()[ind_max])
        E *= np.exp(-1j * phi)
        H *= np.exp(-1j * phi)

        # Rotate back to original coordinates
        (Ex, Ey, Ez) = self._rotate_field_coords(E)
        (Hx, Hy, Hz) = self._rotate_field_coords(H)

        # apply -1 to H fields if a reflection was involved in the rotation
        if self.normal_axis == 1:
            Hx *= -1
            Hy *= -1
            Hz *= -1

        return ((Ex, Ey, Ez), (Hx, Hy, Hz))

    def _field_decay_warning(self, field_data: ModeSolverData):
        """Warn if any of the modes do not decay at the edges."""
        _, plane_dims = self.plane.pop_axis(["x", "y", "z"], axis=self.normal_axis)
        field_sizes = field_data.Ex.sizes
        for freq_index in range(field_sizes["f"]):
            for mode_index in range(field_sizes["mode_index"]):
                e_edge, e_norm = 0, 0
                # Sum up the total field intensity
                for E in (field_data.Ex, field_data.Ey, field_data.Ez):
                    e_norm += np.sum(np.abs(E[{"f": freq_index, "mode_index": mode_index}]) ** 2)
                # Sum up the field intensity at the edges
                if field_sizes[plane_dims[0]] > 1:
                    for E in (field_data.Ex, field_data.Ey, field_data.Ez):
                        isel = {plane_dims[0]: [0, -1], "f": freq_index, "mode_index": mode_index}
                        e_edge += np.sum(np.abs(E[isel]) ** 2)
                if field_sizes[plane_dims[1]] > 1:
                    for E in (field_data.Ex, field_data.Ey, field_data.Ez):
                        isel = {plane_dims[1]: [0, -1], "f": freq_index, "mode_index": mode_index}
                        e_edge += np.sum(np.abs(E[isel]) ** 2)
                # Warn if needed
                if e_edge / e_norm > FIELD_DECAY_CUTOFF:
                    log.warning(
                        f"Mode field at frequency index {freq_index}, mode index {mode_index} does "
                        "not decay at the plane boundaries."
                    )

    @staticmethod
    def _grid_correction(
        simulation: MODE_SIMULATION_TYPE,
        plane: Box,
        mode_spec: ModeSpec,
        n_complex: ModeIndexDataArray,
        direction: Direction,
    ) -> [FreqModeDataArray, FreqModeDataArray]:
        """Correct the fields due to propagation on the grid.

        Return a copy of the :class:`.ModeSolverData` with the fields renormalized to account
        for propagation on a finite grid along the propagation direction. The fields are assumed to
        have ``E exp(1j k r)`` dependence on the finite grid and are then resampled using linear
        interpolation to the exact position of the mode plane. This is needed to correctly compute
        overlap with fields that come from a :class:`.FieldMonitor` placed in the same grid.

        Parameters
        ----------
        grid : :class:`.Grid`
            Numerical grid on which the modes are assumed to propagate.

        Returns
        -------
        :class:`.ModeSolverData`
            Copy of the data with renormalized fields.
        """
        normal_axis = plane.size.index(0.0)
        normal_pos = plane.center[normal_axis]
        normal_dim = "xyz"[normal_axis]

        # Primal and dual grid along the normal direction,
        # i.e. locations of the tangential E-field and H-field components, respectively
        grid = simulation.grid
        normal_primal = grid.boundaries.to_list[normal_axis]
        normal_primal = xr.DataArray(normal_primal, coords={normal_dim: normal_primal})
        normal_dual = grid.centers.to_list[normal_axis]
        normal_dual = xr.DataArray(normal_dual, coords={normal_dim: normal_dual})

        # Propagation phase at the primal and dual locations. The k-vector is along the propagation
        # direction, so angle_theta has to be taken into account. The distance along the propagation
        # direction is the distance along the normal direction over cosine(theta).
        cos_theta = np.cos(mode_spec.angle_theta)
        k_vec = 2 * np.pi * n_complex * n_complex.f / C_0 / cos_theta
        if direction == "-":
            k_vec *= -1
        phase_primal = np.exp(1j * k_vec * (normal_primal - normal_pos))
        phase_dual = np.exp(1j * k_vec * (normal_dual - normal_pos))

        # Fields are modified by a linear interpolation to the exact monitor position
        if normal_primal.size > 1:
            phase_primal = phase_primal.interp(**{normal_dim: normal_pos})
        else:
            phase_primal = phase_primal.squeeze(dim=normal_dim)
        if normal_dual.size > 1:
            phase_dual = phase_dual.interp(**{normal_dim: normal_pos})
        else:
            phase_dual = phase_dual.squeeze(dim=normal_dim)

        return FreqModeDataArray(phase_primal), FreqModeDataArray(phase_dual)

    @property
    def _is_tensorial(self) -> bool:
        """Whether the mode computation should be fully tensorial. This is either due to fully
        anisotropic media, or due to an angled waveguide, in which case the transformed eps and mu
        become tensorial. A separate check is done inside the solver, which looks at the actual
        eps and mu and uses a tolerance to determine whether to invoke the tensorial solver, so
        the actual behavior may differ from what's predicted by this property."""
        return abs(self.mode_spec.angle_theta) > 0 or self._has_fully_anisotropic_media

    @cached_property
    def _intersecting_media(self) -> List:
        """List of media (including simulation background) intersecting the mode plane."""
        total_structures = [self.simulation.scene.background_structure]
        total_structures += list(self.simulation.structures)
        return self.simulation.scene.intersecting_media(self.plane, total_structures)

    @cached_property
    def _has_fully_anisotropic_media(self) -> bool:
        """Check if there are any fully anisotropic media in the plane of the mode."""
        if np.any(
            [isinstance(mat, FullyAnisotropicMedium) for mat in self.simulation.scene.mediums]
        ):
            for int_mat in self._intersecting_media:
                if isinstance(int_mat, FullyAnisotropicMedium):
                    return True
        return False

    @cached_property
    def _has_complex_eps(self) -> bool:
        """Check if there are media with a complex-valued epsilon in the plane of the mode.
        A separate check is done inside the solver, which looks at the actual
        eps and mu and uses a tolerance to determine whether to use real or complex fields, so
        the actual behavior may differ from what's predicted by this property."""
        check_freqs = np.unique([np.amin(self.freqs), np.amax(self.freqs), np.mean(self.freqs)])
        for int_mat in self._intersecting_media:
            for freq in check_freqs:
                max_imag_eps = np.amax(np.abs(np.imag(int_mat.eps_model(freq))))
                if not isclose(max_imag_eps, 0):
                    return False
        return True

    def to_source(
        self,
        source_time: SourceTime,
        direction: Direction = None,
        mode_index: pydantic.NonNegativeInt = 0,
    ) -> ModeSource:
        """Creates :class:`.ModeSource` from a :class:`ModeSolver` instance plus additional
        specifications.

        Parameters
        ----------
        source_time: :class:`.SourceTime`
            Specification of the source time-dependence.
        direction : Direction = None
            Whether source will inject in ``"+"`` or ``"-"`` direction relative to plane normal.
            If not specified, uses the direction from the mode solver.
        mode_index : int = 0
            Index into the list of modes returned by mode solver to use in source.

        Returns
        -------
        :class:`.ModeSource`
            Mode source with specifications taken from the ModeSolver instance and the method
            inputs.
        """

        if direction is None:
            direction = self.direction

        return ModeSource(
            center=self.plane.center,
            size=self.plane.size,
            source_time=source_time,
            mode_spec=self.mode_spec,
            mode_index=mode_index,
            direction=direction,
        )

    def to_monitor(self, freqs: List[float] = None, name: str = None) -> ModeMonitor:
        """Creates :class:`ModeMonitor` from a :class:`ModeSolver` instance plus additional
        specifications.

        Parameters
        ----------
        freqs : List[float]
            Frequencies to include in Monitor (Hz).
            If not specified, passes ``self.freqs``.
        name : str
            Required name of monitor.

        Returns
        -------
        :class:`.ModeMonitor`
            Mode monitor with specifications taken from the ModeSolver instance and the method
            inputs.
        """

        if freqs is None:
            freqs = self.freqs

        if name is None:
            raise ValueError(
                "A 'name' must be passed to 'ModeSolver.to_monitor'. "
                "The default value of 'None' is for backwards compatibility and is not accepted."
            )

        return ModeMonitor(
            center=self.plane.center,
            size=self.plane.size,
            freqs=freqs,
            mode_spec=self.mode_spec,
            name=name,
        )

    def to_mode_solver_monitor(self, name: str, colocate: bool = None) -> ModeSolverMonitor:
        """Creates :class:`ModeSolverMonitor` from a :class:`ModeSolver` instance.

        Parameters
        ----------
        name : str
            Name of the monitor.
        colocate : bool
            Whether to colocate fields or compute on the Yee grid. If not provided, the value
            set in the :class:`ModeSolver` instance is used.

        Returns
        -------
        :class:`.ModeSolverMonitor`
            Mode monitor with specifications taken from the ModeSolver instance and ``name``.
        """

        if colocate is None:
            colocate = self.colocate

        return ModeSolverMonitor(
            size=self.plane.size,
            center=self.plane.center,
            mode_spec=self.mode_spec,
            freqs=self.freqs,
            direction=self.direction,
            colocate=colocate,
            name=name,
        )

    @require_fdtd_simulation
    def sim_with_source(
        self,
        source_time: SourceTime,
        direction: Direction = None,
        mode_index: pydantic.NonNegativeInt = 0,
    ) -> Simulation:
        """Creates :class:`Simulation` from a :class:`ModeSolver`. Creates a copy of
        the ModeSolver's original simulation with a ModeSource added corresponding to
        the ModeSolver parameters.

        Parameters
        ----------
        source_time: :class:`.SourceTime`
            Specification of the source time-dependence.
        direction : Direction = None
            Whether source will inject in ``"+"`` or ``"-"`` direction relative to plane normal.
            If not specified, uses the direction from the mode solver.
        mode_index : int = 0
            Index into the list of modes returned by mode solver to use in source.

        Returns
        -------
        :class:`.Simulation`
            Copy of the simulation with a :class:`.ModeSource` with specifications taken
            from the ModeSolver instance and the method inputs.
        """

        mode_source = self.to_source(
            mode_index=mode_index, direction=direction, source_time=source_time
        )
        new_sources = list(self.simulation.sources) + [mode_source]
        new_sim = self.simulation.updated_copy(sources=new_sources)
        return new_sim

    @require_fdtd_simulation
    def sim_with_monitor(
        self,
        freqs: List[float] = None,
        name: str = None,
    ) -> Simulation:
        """Creates :class:`.Simulation` from a :class:`ModeSolver`. Creates a copy of
        the ModeSolver's original simulation with a mode monitor added corresponding to
        the ModeSolver parameters.

        Parameters
        ----------
        freqs : List[float] = None
            Frequencies to include in Monitor (Hz).
            If not specified, uses the frequencies from the mode solver.
        name : str
            Required name of monitor.

        Returns
        -------
        :class:`.Simulation`
            Copy of the simulation with a :class:`.ModeMonitor` with specifications taken
            from the ModeSolver instance and the method inputs.
        """

        mode_monitor = self.to_monitor(freqs=freqs, name=name)
        new_monitors = list(self.simulation.monitors) + [mode_monitor]
        new_sim = self.simulation.updated_copy(monitors=new_monitors)
        return new_sim

    def sim_with_mode_solver_monitor(
        self,
        name: str,
    ) -> Simulation:
        """Creates :class:`Simulation` from a :class:`ModeSolver`. Creates a
        copy of the ModeSolver's original simulation with a mode solver monitor
        added corresponding to the ModeSolver parameters.

        Parameters
        ----------
        name : str
            Name of the monitor.

        Returns
        -------
        :class:`.Simulation`
            Copy of the simulation with a :class:`.ModeSolverMonitor` with specifications taken
            from the ModeSolver instance and ``name``.
        """
        mode_solver_monitor = self.to_mode_solver_monitor(name=name)
        new_monitors = list(self.simulation.monitors) + [mode_solver_monitor]
        new_sim = self.simulation.updated_copy(monitors=new_monitors)
        return new_sim

    def plot_field(
        self,
        field_name: str,
        val: Literal["real", "imag", "abs"] = "real",
        scale: PlotScale = "lin",
        eps_alpha: float = 0.2,
        robust: bool = True,
        vmin: float = None,
        vmax: float = None,
        ax: Ax = None,
        **sel_kwargs,
    ) -> Ax:
        """Plot the field for a :class:`.ModeSolverData` with :class:`.Simulation` plot overlaid.

        Parameters
        ----------
        field_name : str
            Name of ``field`` component to plot (eg. ``'Ex'``).
            Also accepts ``'E'`` and ``'H'`` to plot the vector magnitudes of the electric and
            magnetic fields, and ``'S'`` for the Poynting vector.
        val : Literal['real', 'imag', 'abs', 'abs^2', 'dB'] = 'real'
            Which part of the field to plot.
        eps_alpha : float = 0.2
            Opacity of the structure permittivity.
            Must be between 0 and 1 (inclusive).
        robust : bool = True
            If True and vmin or vmax are absent, uses the 2nd and 98th percentiles of the data
            to compute the color limits. This helps in visualizing the field patterns especially
            in the presence of a source.
        vmin : float = None
            The lower bound of data range that the colormap covers. If ``None``, they are
            inferred from the data and other keyword arguments.
        vmax : float = None
            The upper bound of data range that the colormap covers. If ``None``, they are
            inferred from the data and other keyword arguments.
        ax : matplotlib.axes._subplots.Axes = None
            matplotlib axes to plot on, if not specified, one is created.
        sel_kwargs : keyword arguments used to perform ``.sel()`` selection in the monitor data.
            These kwargs can select over the spatial dimensions (``x``, ``y``, ``z``),
            frequency or time dimensions (``f``, ``t``) or `mode_index`, if applicable.
            For the plotting to work appropriately, the resulting data after selection must contain
            only two coordinates with len > 1.
            Furthermore, these should be spatial coordinates (``x``, ``y``, or ``z``).

        Returns
        -------
        matplotlib.axes._subplots.Axes
            The supplied or created matplotlib axes.
        """

        sim_data = self.sim_data
        return sim_data.plot_field(
            field_monitor_name=MODE_MONITOR_NAME,
            field_name=field_name,
            val=val,
            scale=scale,
            eps_alpha=eps_alpha,
            robust=robust,
            vmin=vmin,
            vmax=vmax,
            ax=ax,
            **sel_kwargs,
        )

    def plot(
        self,
        ax: Ax = None,
        **patch_kwargs,
    ) -> Ax:
        """Plot the mode plane simulation's components.

        Parameters
        ----------
        ax : matplotlib.axes._subplots.Axes = None
            Matplotlib axes to plot on, if not specified, one is created.

        Returns
        -------
        matplotlib.axes._subplots.Axes
            The supplied or created matplotlib axes.

        See Also
        ---------

        **Notebooks**
            * `Visualizing geometries in Tidy3D: Plotting Materials <../../notebooks/VizSimulation.html#Plotting-Materials>`_

        """
        # Get the mode plane normal axis, center, and limits.
        a_center, h_lim, v_lim, _ = self._center_and_lims()

        return self.simulation.plot(
            x=a_center[0],
            y=a_center[1],
            z=a_center[2],
            hlim=h_lim,
            vlim=v_lim,
            source_alpha=0,
            monitor_alpha=0,
            lumped_element_alpha=0,
            ax=ax,
            **patch_kwargs,
        )

    def plot_eps(
        self,
        freq: float = None,
        alpha: float = None,
        ax: Ax = None,
    ) -> Ax:
        """Plot the mode plane simulation's components.
        The permittivity is plotted in grayscale based on its value at the specified frequency.

        Parameters
        ----------
        freq : float = None
            Frequency to evaluate the relative permittivity of all mediums.
            If not specified, evaluates at infinite frequency.
        alpha : float = None
            Opacity of the structures being plotted.
            Defaults to the structure default alpha.
        ax : matplotlib.axes._subplots.Axes = None
            Matplotlib axes to plot on, if not specified, one is created.

        Returns
        -------
        matplotlib.axes._subplots.Axes
            The supplied or created matplotlib axes.

        See Also
        ---------

        **Notebooks**
            * `Visualizing geometries in Tidy3D: Plotting Permittivity <../../notebooks/VizSimulation.html#Plotting-Permittivity>`_
        """

        # Get the mode plane normal axis, center, and limits.
        a_center, h_lim, v_lim, _ = self._center_and_lims()

        # Plot at central mode frequency if freq is not provided.
        f = freq if freq is not None else self.freqs[len(self.freqs) // 2]

        return self.simulation.plot_eps(
            x=a_center[0],
            y=a_center[1],
            z=a_center[2],
            freq=f,
            alpha=alpha,
            hlim=h_lim,
            vlim=v_lim,
            source_alpha=0,
            monitor_alpha=0,
            lumped_element_alpha=0,
            ax=ax,
        )

    def plot_structures_eps(
        self,
        freq: float = None,
        alpha: float = None,
        cbar: bool = True,
        reverse: bool = False,
        ax: Ax = None,
    ) -> Ax:
        """Plot the mode plane simulation's components.
        The permittivity is plotted in grayscale based on its value at the specified frequency.

        Parameters
        ----------
        freq : float = None
            Frequency to evaluate the relative permittivity of all mediums.
            If not specified, evaluates at infinite frequency.
        alpha : float = None
            Opacity of the structures being plotted.
            Defaults to the structure default alpha.
        cbar : bool = True
            Whether to plot a colorbar for the relative permittivity.
        reverse : bool = False
            If ``False``, the highest permittivity is plotted in black.
            If ``True``, it is plotteed in white (suitable for black backgrounds).
        ax : matplotlib.axes._subplots.Axes = None
            Matplotlib axes to plot on, if not specified, one is created.

        Returns
        -------
        matplotlib.axes._subplots.Axes
            The supplied or created matplotlib axes.

        See Also
        ---------

        **Notebooks**
            * `Visualizing geometries in Tidy3D: Plotting Permittivity <../../notebooks/VizSimulation.html#Plotting-Permittivity>`_
        """

        # Get the mode plane normal axis, center, and limits.
        a_center, h_lim, v_lim, _ = self._center_and_lims()

        # Plot at central mode frequency if freq is not provided.
        f = freq if freq is not None else self.freqs[len(self.freqs) // 2]

        return self.simulation.plot_structures_eps(
            x=a_center[0],
            y=a_center[1],
            z=a_center[2],
            freq=f,
            alpha=alpha,
            cbar=cbar,
            reverse=reverse,
            hlim=h_lim,
            vlim=v_lim,
            ax=ax,
        )

    def plot_grid(
        self,
        ax: Ax = None,
        **kwargs,
    ) -> Ax:
        """Plot the mode plane cell boundaries as lines.

        Parameters
        ----------
        ax : matplotlib.axes._subplots.Axes = None
            Matplotlib axes to plot on, if not specified, one is created.
        **kwargs
            Optional keyword arguments passed to the matplotlib ``LineCollection``.
            For details on accepted values, refer to
            `Matplotlib's documentation <https://tinyurl.com/2p97z4cn>`_.

        Returns
        -------
        matplotlib.axes._subplots.Axes
            The supplied or created matplotlib axes.
        """

        # Get the mode plane normal axis, center, and limits.
        a_center, h_lim, v_lim, _ = self._center_and_lims()

        return self.simulation.plot_grid(
            x=a_center[0], y=a_center[1], z=a_center[2], hlim=h_lim, vlim=v_lim, ax=ax, **kwargs
        )

    def plot_pml(
        self,
        ax: Ax = None,
    ) -> Ax:
        """Plot the mode plane absorbing boundaries.

        Parameters
        ----------
        ax : matplotlib.axes._subplots.Axes = None
            Matplotlib axes to plot on, if not specified, one is created.

        Returns
        -------
        matplotlib.axes._subplots.Axes
            The supplied or created matplotlib axes.
        """

        # Get the mode plane normal axis, center, and limits.
        a_center, h_lim, v_lim, t_axes = self._center_and_lims()

        # Plot the mode plane is ax=None.
        if not ax:
            ax = self.simulation.plot(
                x=a_center[0],
                y=a_center[1],
                z=a_center[2],
                hlim=h_lim,
                vlim=v_lim,
                source_alpha=0,
                monitor_alpha=0,
                ax=ax,
            )

        # Mode plane grid.
        plane_grid = self.grid_snapped.centers.to_list
        coord_0 = plane_grid[t_axes[0]][1:-1]
        coord_1 = plane_grid[t_axes[1]][1:-1]

        # Number of PML layers in ModeSpec.
        num_pml_0 = self.mode_spec.num_pml[0]
        num_pml_1 = self.mode_spec.num_pml[1]

        # Calculate PML thickness.
        pml_thick_0_plus = 0
        pml_thick_0_minus = 0
        if num_pml_0 > 0:
            pml_thick_0_plus = coord_0[-1] - coord_0[-num_pml_0 - 1]
            pml_thick_0_minus = coord_0[num_pml_0] - coord_0[0]
            if self.solver_symmetry[0] != 0:
                pml_thick_0_minus = pml_thick_0_plus

        pml_thick_1_plus = 0
        pml_thick_1_minus = 0
        if num_pml_1 > 0:
            pml_thick_1_plus = coord_1[-1] - coord_1[-num_pml_1 - 1]
            pml_thick_1_minus = coord_1[num_pml_1] - coord_1[0]
            if self.solver_symmetry[1] != 0:
                pml_thick_1_minus = pml_thick_1_plus

        # Mode Plane width and height
        mp_w = h_lim[1] - h_lim[0]
        mp_h = v_lim[1] - v_lim[0]

        # Plot the absorbing layers.
        if num_pml_0 > 0 or num_pml_1 > 0:
            pml_rect = []
            if pml_thick_0_minus > 0:
                pml_rect.append(Rectangle((h_lim[0], v_lim[0]), pml_thick_0_minus, mp_h))
            if pml_thick_0_plus > 0:
                pml_rect.append(
                    Rectangle((h_lim[1] - pml_thick_0_plus, v_lim[0]), pml_thick_0_plus, mp_h)
                )
            if pml_thick_1_minus > 0:
                pml_rect.append(Rectangle((h_lim[0], v_lim[0]), mp_w, pml_thick_1_minus))
            if pml_thick_1_plus > 0:
                pml_rect.append(
                    Rectangle((h_lim[0], v_lim[1] - pml_thick_1_plus), mp_w, pml_thick_1_plus)
                )

            pc = PatchCollection(
                pml_rect,
                alpha=plot_params_pml.alpha,
                facecolor=plot_params_pml.facecolor,
                edgecolor=plot_params_pml.edgecolor,
                hatch=plot_params_pml.hatch,
                zorder=plot_params_pml.zorder,
            )
            ax.add_collection(pc)

        return ax

    def _center_and_lims(self) -> Tuple[List, List, List, List]:
        """Get the mode plane center and limits."""

        n_axis, t_axes = self.plane.pop_axis([0, 1, 2], self.normal_axis)
        a_center = [None, None, None]
        a_center[n_axis] = self.plane.center[n_axis]

        _, (h_min_s, v_min_s) = Box.pop_axis(self.simulation.bounds[0], axis=n_axis)
        _, (h_max_s, v_max_s) = Box.pop_axis(self.simulation.bounds[1], axis=n_axis)

        h_min = a_center[n_axis] - self.plane.size[t_axes[0]] / 2
        h_max = a_center[n_axis] + self.plane.size[t_axes[0]] / 2
        v_min = a_center[n_axis] - self.plane.size[t_axes[1]] / 2
        v_max = a_center[n_axis] + self.plane.size[t_axes[1]] / 2

        h_lim = [
            h_min if abs(h_min) < abs(h_min_s) else h_min_s,
            h_max if abs(h_max) < abs(h_max_s) else h_max_s,
        ]
        v_lim = [
            v_min if abs(v_min) < abs(v_min_s) else v_min_s,
            v_max if abs(v_max) < abs(v_max_s) else v_max_s,
        ]

        return a_center, h_lim, v_lim, t_axes

    def _validate_modes_size(self):
        """Make sure that the total size of the modes fields is not too large."""
        monitor = self.to_mode_solver_monitor(name=MODE_MONITOR_NAME)
        num_cells = self.simulation._monitor_num_cells(monitor)
        # size in GB
        total_size = monitor._storage_size_solver(num_cells=num_cells, tmesh=[]) / 1e9
        if total_size > MAX_MODES_DATA_SIZE_GB:
            raise SetupError(
                f"Mode solver has {total_size:.2f}GB of estimated storage, "
                f"a maximum of {MAX_MODES_DATA_SIZE_GB:.2f}GB is allowed. Consider making the "
                "mode plane smaller, or decreasing the resolution or number of requested "
                "frequencies or modes."
            )

    def validate_pre_upload(self, source_required: bool = True):
        self._validate_modes_size()

    @cached_property
    def reduced_simulation_copy(self):
        """Strip objects not used by the mode solver from simulation object.
        This might significantly reduce upload time in the presence of custom mediums.
        """

        # for now, we handle EME simulation by converting to FDTD simulation
        # because we can't take planar subsection of an EME simulation.
        # eventually, we will convert to ModeSimulation
        if isinstance(self.simulation, EMESimulation):
            return self.to_fdtd_mode_solver().reduced_simulation_copy

        # we preserve extra cells along the normal direction to ensure there is enough data for
        # subpixel
        extended_grid = self._get_solver_grid(keep_additional_layers=True, truncate_symmetry=False)
        grids_1d = extended_grid.boundaries
        new_sim_box = Box.from_bounds(
            rmin=(grids_1d.x[0], grids_1d.y[0], grids_1d.z[0]),
            rmax=(grids_1d.x[-1], grids_1d.y[-1], grids_1d.z[-1]),
        )

        # remove PML, Absorers, etc, to avoid unnecessary cells
        bspec = self.simulation.boundary_spec

        new_bspec_dict = {}
        for axis in "xyz":
            bcomp = bspec[axis]
            for bside, sign in zip([bcomp.plus, bcomp.minus], "+-"):
                if isinstance(bside, (PML, StablePML, Absorber)):
                    new_bspec_dict[axis + sign] = PECBoundary()
                else:
                    new_bspec_dict[axis + sign] = bside

        new_bspec = BoundarySpec(
            x=Boundary(plus=new_bspec_dict["x+"], minus=new_bspec_dict["x-"]),
            y=Boundary(plus=new_bspec_dict["y+"], minus=new_bspec_dict["y-"]),
            z=Boundary(plus=new_bspec_dict["z+"], minus=new_bspec_dict["z-"]),
        )

        # extract sub-simulation removing everything irrelevant
        new_sim = self.simulation.subsection(
            region=new_sim_box,
            monitors=[],
            sources=[],
            grid_spec="identical",
            boundary_spec=new_bspec,
            remove_outside_custom_mediums=True,
            remove_outside_structures=True,
            include_pml_cells=True,
        )

        return self.updated_copy(simulation=new_sim)

    def to_fdtd_mode_solver(self) -> ModeSolver:
        """Construct a new :class:`.ModeSolver` by converting ``simulation``
        from a :class:`.EMESimulation` to an FDTD :class:`.Simulation`.
        Only used as a workaround until :class:`.EMESimulation` is natively supported in the
        :class:`.ModeSolver` webapi."""
        if not isinstance(self.simulation, EMESimulation):
            raise ValidationError(
                "The method 'to_fdtd_mode_solver' is only needed "
                "when the 'simulation' is an 'EMESimulation'."
            )
        fdtd_sim = self.simulation._to_fdtd_sim()
        return self.updated_copy(simulation=fdtd_sim)
