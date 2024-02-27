import numpy as np
from numba import njit

from cellfinder.core.tools.array_operations import bin_mean_3d
from cellfinder.core.tools.geometry import make_sphere

DEBUG = False


class BallFilter:
    """
    A 3D ball filter.

    This runs a spherical kernel across the (x, y) dimensions
    of a *ball_z_size* stack of planes, and marks pixels in the middle
    plane of the stack that have a high enough intensity within the
    spherical kernel.
    """

    def __init__(
        self,
        plane_width: int,
        plane_height: int,
        ball_xy_size: int,
        ball_z_size: int,
        overlap_fraction: float,
        tile_step_width: int,
        tile_step_height: int,
        threshold_value: int,
        soma_centre_value: int,
    ):
        """
        Parameters
        ----------
        plane_width, plane_height :
            Width/height of the planes.
        ball_xy_size :
            Diameter of the spherical kernel in the x/y dimensions.
        ball_z_size :
            Diameter of the spherical kernel in the z dimension.
            Equal to the number of planes that stacked to filter
            the central plane of the stack.
        overlap_fraction :
            The fraction of pixels within the spherical kernel that
            have to be over *threshold_value* for a pixel to be marked
            as having a high intensity.
        tile_step_width, tile_step_height :
            Width/height of individual tiles in the mask generated by
            2D filtering.
        threshold_value :
            Value above which an individual pixel is considered to have
            a high intensity.
        soma_centre_value :
            Value used to mark pixels with a high enough intensity.
        """
        self.ball_xy_size = ball_xy_size
        self.ball_z_size = ball_z_size
        self.overlap_fraction = overlap_fraction
        self.tile_step_width = tile_step_width
        self.tile_step_height = tile_step_height

        self.THRESHOLD_VALUE = threshold_value
        self.SOMA_CENTRE_VALUE = soma_centre_value

        # Create a spherical kernel.
        #
        # This is done by:
        # 1. Generating a binary sphere at a resolution *upscale_factor* larger
        #    than desired.
        # 2. Downscaling the binary sphere to get a 'fuzzy' sphere at the
        #    original intended scale
        upscale_factor: int = 7
        upscaled_kernel_shape = (
            upscale_factor * ball_xy_size,
            upscale_factor * ball_xy_size,
            upscale_factor * ball_z_size,
        )
        upscaled_ball_centre_position = (
            np.floor(upscaled_kernel_shape[0] / 2),
            np.floor(upscaled_kernel_shape[1] / 2),
            np.floor(upscaled_kernel_shape[2] / 2),
        )
        upscaled_ball_radius = upscaled_kernel_shape[0] / 2.0
        sphere_kernel = make_sphere(
            upscaled_kernel_shape,
            upscaled_ball_radius,
            upscaled_ball_centre_position,
        )
        sphere_kernel = sphere_kernel.astype(np.float64)
        self.kernel = bin_mean_3d(
            sphere_kernel,
            bin_height=upscale_factor,
            bin_width=upscale_factor,
            bin_depth=upscale_factor,
        )

        assert (
            self.kernel.shape[2] == ball_z_size
        ), "Kernel z dimension should be {}, got {}".format(
            ball_z_size, self.kernel.shape[2]
        )

        self.overlap_threshold = np.sum(self.overlap_fraction * self.kernel)

        # Stores the current planes that are being filtered
        self.volume = np.empty(
            (plane_width, plane_height, ball_z_size), dtype=np.uint32
        )
        # Index of the middle plane in the volume
        self.middle_z_idx = int(np.floor(ball_z_size / 2))

        # TODO: lazy initialisation
        self.inside_brain_tiles = np.empty(
            (
                int(np.ceil(plane_width / tile_step_width)),
                int(np.ceil(plane_height / tile_step_height)),
                ball_z_size,
            ),
            dtype=bool,
        )
        # Stores the z-index in volume at which new planes are inserted when
        # append() is called
        self.__current_z = -1

    @property
    def ready(self) -> bool:
        """
        Return `True` if enough planes have been appended to run the filter.
        """
        return self.__current_z == self.ball_z_size - 1

    def append(self, plane: np.ndarray, mask: np.ndarray) -> None:
        """
        Add a new 2D plane to the filter.
        """
        if DEBUG:
            assert [e for e in plane.shape[:2]] == [
                e for e in self.volume.shape[:2]
            ], 'plane shape mismatch, expected "{}", got "{}"'.format(
                [e for e in self.volume.shape[:2]],
                [e for e in plane.shape[:2]],
            )
            assert [e for e in mask.shape[:2]] == [
                e for e in self.inside_brain_tiles.shape[:2]
            ], 'mask shape mismatch, expected"{}", got {}"'.format(
                [e for e in self.inside_brain_tiles.shape[:2]],
                [e for e in mask.shape[:2]],
            )
        if not self.ready:
            self.__current_z += 1
        else:
            # Shift everything down by one to make way for the new plane
            self.volume = np.roll(
                self.volume, -1, axis=2
            )  # WARNING: not in place
            self.inside_brain_tiles = np.roll(
                self.inside_brain_tiles, -1, axis=2
            )
        # Add the new plane to the top of volume and inside_brain_tiles
        self.volume[:, :, self.__current_z] = plane[:, :]
        self.inside_brain_tiles[:, :, self.__current_z] = mask[:, :]

    def get_middle_plane(self) -> np.ndarray:
        """
        Get the plane in the middle of self.volume.
        """
        z = self.middle_z_idx
        return np.array(self.volume[:, :, z], dtype=np.uint32)

    def walk(self) -> None:  # Highly optimised because most time critical
        ball_radius = self.ball_xy_size // 2
        # Get extents of image that are covered by tiles
        tile_mask_covered_img_width = (
            self.inside_brain_tiles.shape[0] * self.tile_step_width
        )
        tile_mask_covered_img_height = (
            self.inside_brain_tiles.shape[1] * self.tile_step_height
        )
        # Get maximum offsets for the ball
        max_width = tile_mask_covered_img_width - self.ball_xy_size
        max_height = tile_mask_covered_img_height - self.ball_xy_size

        _walk(
            max_height,
            max_width,
            self.tile_step_width,
            self.tile_step_height,
            self.inside_brain_tiles,
            self.volume,
            self.kernel,
            ball_radius,
            self.middle_z_idx,
            self.overlap_threshold,
            self.THRESHOLD_VALUE,
            self.SOMA_CENTRE_VALUE,
        )


@njit(cache=True)
def _cube_overlaps(
    cube: np.ndarray,
    overlap_threshold: float,
    THRESHOLD_VALUE: int,
    kernel: np.ndarray,
) -> bool:  # Highly optimised because most time critical
    """
    For each pixel in cube that is greater than THRESHOLD_VALUE, sum
    up the corresponding pixels in *kernel*. If the total is less than
    overlap_threshold, return False, otherwise return True.

    Halfway through scanning the z-planes, if the total overlap is
    less than 0.4 * overlap_threshold, this will return False early
    without scanning the second half of the z-planes.

    Parameters
    ----------
    cube :
        3D array.
    overlap_threshold :
        Threshold above which to return True.
    THRESHOLD_VALUE :
        Value above which a pixel is marked as being part of a cell.
    kernel :
        3D array, with the same shape as *cube*.
    """
    current_overlap_value = 0

    middle = np.floor(cube.shape[2] / 2) + 1
    halfway_overlap_thresh = (
        overlap_threshold * 0.4
    )  # FIXME: do not hard code value

    for z in range(cube.shape[2]):
        # TODO: OPTIMISE: step from middle to outer boundaries to check
        # more data first
        #
        # If halfway through the array, and the overlap value isn't more than
        # 0.4 * the overlap threshold, return
        if z == middle and current_overlap_value < halfway_overlap_thresh:
            return False  # DEBUG: optimisation attempt
        for y in range(cube.shape[1]):
            for x in range(cube.shape[0]):
                # includes self.SOMA_CENTRE_VALUE
                if cube[x, y, z] >= THRESHOLD_VALUE:
                    current_overlap_value += kernel[x, y, z]
    return current_overlap_value > overlap_threshold


@njit
def _is_tile_to_check(
    x: int,
    y: int,
    middle_z: int,
    tile_step_width: int,
    tile_step_height: int,
    inside_brain_tiles: np.ndarray,
) -> bool:  # Highly optimised because most time critical
    """
    Check if the tile containing pixel (x, y) is a tile that needs checking.
    """
    x_in_mask = x // tile_step_width  # TEST: test bounds (-1 range)
    y_in_mask = y // tile_step_height  # TEST: test bounds (-1 range)
    return inside_brain_tiles[x_in_mask, y_in_mask, middle_z]


@njit
def _walk(
    max_height: int,
    max_width: int,
    tile_step_width: int,
    tile_step_height: int,
    inside_brain_tiles: np.ndarray,
    volume: np.ndarray,
    kernel: np.ndarray,
    ball_radius: int,
    middle_z: int,
    overlap_threshold: float,
    THRESHOLD_VALUE: int,
    SOMA_CENTRE_VALUE: int,
) -> None:
    """
    Scan through *volume*, and mark pixels where there are enough surrounding
    pixels with high enough intensity.

    The surrounding area is defined by the *kernel*.

    Parameters
    ----------
    max_height, max_width :
        Maximum offsets for the ball filter.
    inside_brain_tiles :
        Array containing information on whether a tile is inside the brain
        or not. Tiles outside the brain are skipped.
    volume :
        3D array containing the plane-filtered data.
    kernel :
        3D array
    ball_radius :
        Radius of the ball in the xy plane.
    SOMA_CENTRE_VALUE :
        Value that is used to mark pixels in *volume*.

    Notes
    -----
    Warning: modifies volume in place!
    """
    for y in range(max_height):
        for x in range(max_width):
            ball_centre_x = x + ball_radius
            ball_centre_y = y + ball_radius
            if _is_tile_to_check(
                ball_centre_x,
                ball_centre_y,
                middle_z,
                tile_step_width,
                tile_step_height,
                inside_brain_tiles,
            ):
                cube = volume[
                    x : x + kernel.shape[0],
                    y : y + kernel.shape[1],
                    :,
                ]
                if _cube_overlaps(
                    cube,
                    overlap_threshold,
                    THRESHOLD_VALUE,
                    kernel,
                ):
                    volume[ball_centre_x, ball_centre_y, middle_z] = (
                        SOMA_CENTRE_VALUE
                    )
