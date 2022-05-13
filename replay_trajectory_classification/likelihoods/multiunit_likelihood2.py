import numpy as np
from replay_trajectory_classification.bins import atleast_2d
from replay_trajectory_classification.likelihoods.diffusion import (
    diffuse_each_bin, estimate_diffusion_position_density,
    estimate_diffusion_position_distance)
from tqdm.autonotebook import tqdm


def gaussian_pdf(x, mean, sigma):
    '''Compute the value of a Gaussian probability density function at x with
    given mean and sigma.'''
    return np.exp(-0.5 * ((x - mean) / sigma)**2) / (sigma * np.sqrt(2.0 * np.pi))


def estimate_position_distance(place_bin_centers, positions, position_std):
    '''



    Parameters
    ----------
    place_bin_centers : ndarray, shape (n_position_bins, n_position_dims)
    positions : ndarray, shape (n_time, n_position_dims)
    position_std : float

    Returns
    -------
    position_distance : ndarray, shape (n_time, n_position_bins)

    '''
    n_time, n_position_dims = positions.shape
    n_position_bins = place_bin_centers.shape[0]

    position_distance = np.ones(
        (n_time, n_position_bins), dtype=np.float32)

    for position_ind in range(n_position_dims):
        position_distance *= gaussian_pdf(
            np.expand_dims(place_bin_centers[:, position_ind], axis=0),
            np.expand_dims(positions[:, position_ind], axis=1),
            position_std)

    return position_distance


def estimate_position_density(place_bin_centers, positions, position_std,
                              block_size=100):
    '''

    Parameters
    ----------
    place_bin_centers : ndarray, shape (n_position_bins, n_position_dims)
    positions : ndarray, shape (n_time, n_position_dims)
    position_std : float

    Returns
    -------
    position_density : ndarray, shape (n_position_bins,)

    '''
    n_time = positions.shape[0]
    n_position_bins = place_bin_centers.shape[0]

    if block_size is None:
        block_size = n_time

    position_density = np.empty((n_position_bins,))
    for start_ind in range(0, n_position_bins, block_size):
        block_inds = slice(start_ind, start_ind + block_size)
        position_density[block_inds] = np.mean(estimate_position_distance(
            place_bin_centers[block_inds], positions, position_std), axis=0)
    return position_density


def estimate_log_intensity(density, occupancy, mean_rate):
    return np.log(mean_rate) + np.log(density) - np.log(occupancy)


def estimate_intensity(density, occupancy, mean_rate):
    '''

    Parameters
    ----------
    density : ndarray, shape (n_bins,)
    occupancy : ndarray, shape (n_bins,)
    mean_rate : float

    Returns
    -------
    intensity : ndarray, shape (n_bins,)

    '''
    return np.exp(estimate_log_intensity(density, occupancy, mean_rate))


def estimate_log_joint_mark_intensity(decoding_marks,
                                      encoding_marks,
                                      mark_std,
                                      occupancy,
                                      mean_rate,
                                      place_bin_centers=None,
                                      encoding_positions=None,
                                      position_std=None,
                                      max_mark_value=6000,
                                      set_diag_zero=False,
                                      position_distance=None):
    """

    Parameters
    ----------
    decoding_marks : ndarray, shape (n_decoding_spikes, n_features)
    encoding_marks : ndarray, shape (n_encoding_spikes, n_features)
    mark_std : float or ndarray, shape (n_features,)
    occupancy : ndarray, shape (n_position_bins,)
    mean_rate : float
    place_bin_centers : ndarray, shape (n_position_bins, n_position_dims)
    encoding_positions : ndarray, shape (n_decoding_spikes, n_position_dims)
    position_std : float
    is_track_interior : None or ndarray, shape (n_position_bins,)
    max_mark_value : int
    set_diag_zero : bool

    Returns
    -------
    log_joint_mark_intensity : ndarray, shape (n_decoding_spikes, n_position_bins)

    """
    n_encoding_spikes, n_marks = encoding_marks.shape
    n_decoding_spikes = decoding_marks.shape[0]
    mark_distance = np.ones(
        (n_decoding_spikes, n_encoding_spikes), dtype=np.float32)

    for mark_ind in range(n_marks):
        mark_distance *= gaussian_pdf(
            np.expand_dims(decoding_marks[:, mark_ind], axis=1),
            np.expand_dims(encoding_marks[:, mark_ind], axis=0),
            mark_std)

    if set_diag_zero:
        diag_ind = (np.arange(n_decoding_spikes),
                    np.arange(n_decoding_spikes))
        mark_distance[diag_ind] = 0.0

    if position_distance is None:
        position_distance = estimate_position_distance(
            place_bin_centers, encoding_positions, position_std
        ).astype(np.float32)

    return estimate_log_intensity(
        mark_distance @ position_distance / n_encoding_spikes,
        occupancy,
        mean_rate)


def fit_multiunit_likelihood2(position,
                              multiunits,
                              place_bin_centers,
                              mark_std,
                              position_std,
                              is_track_boundary=None,
                              is_track_interior=None,
                              edges=None,
                              block_size=100,
                              use_diffusion_distance=False,
                              **kwargs):
    '''

    Parameters
    ----------
    position : ndarray, shape (n_time, n_position_dims)
    multiunits : ndarray, shape (n_time, n_marks, n_electrodes)
    place_bin_centers : ndarray, shape ( n_bins, n_position_dims)
    model : sklearn model
    model_kwargs : dict
    occupancy_model : sklearn model
    occupancy_kwargs : dict
    is_track_interior : None or ndarray, shape (n_bins,)

    Returns
    -------
    joint_pdf_models : list of sklearn models, shape (n_electrodes,)
    ground_process_intensities : list of ndarray, shape (n_electrodes,)
    occupancy : ndarray, (n_bins, n_position_dims)
    mean_rates : ndarray, (n_electrodes,)

    '''
    if is_track_interior is None:
        is_track_interior = np.ones((place_bin_centers.shape[0],),
                                    dtype=np.bool)

    position = atleast_2d(position)
    place_bin_centers = atleast_2d(place_bin_centers)
    interior_place_bin_centers = np.asarray(
        place_bin_centers[is_track_interior.ravel(order='F')],
        dtype=np.float32)
    gpu_is_track_interior = np.asarray(is_track_interior.ravel(order='F'))

    not_nan_position = np.all(~np.isnan(position), axis=1)

    if use_diffusion_distance & (position.shape[1] > 1):
        n_total_bins = np.prod(is_track_interior.shape)
        bin_diffusion_distances = diffuse_each_bin(
            is_track_interior,
            is_track_boundary,
            dx=edges[0][1] - edges[0][0],
            dy=edges[1][1] - edges[1][0],
            std=position_std,
        ).reshape((n_total_bins, -1), order='F')
    else:
        bin_diffusion_distances = None

    if use_diffusion_distance & (position.shape[1] > 1):
        occupancy = np.asarray(
            estimate_diffusion_position_density(
                position[not_nan_position],
                edges,
                bin_distances=bin_diffusion_distances,
            ), dtype=np.float32)
    else:
        occupancy = np.zeros(
            (place_bin_centers.shape[0],), dtype=np.float32)
        occupancy[gpu_is_track_interior] = estimate_position_density(
            interior_place_bin_centers,
            np.asarray(position[not_nan_position], dtype=np.float32),
            position_std, block_size=block_size)

    mean_rates = []
    summed_ground_process_intensity = np.zeros(
        (place_bin_centers.shape[0],), dtype=np.float32)
    encoding_marks = []
    encoding_positions = []

    for multiunit in np.moveaxis(multiunits, -1, 0):

        # ground process intensity
        is_spike = np.any(~np.isnan(multiunit), axis=1)
        mean_rates.append(is_spike.mean())

        if is_spike.sum() > 0:
            if use_diffusion_distance & (position.shape[1] > 1):
                marginal_density = np.asarray(
                    estimate_diffusion_position_density(
                        position[is_spike & not_nan_position],
                        edges,
                        bin_distances=bin_diffusion_distances
                    ), dtype=np.float32)
            else:
                marginal_density = np.zeros(
                    (place_bin_centers.shape[0],), dtype=np.float32)
                marginal_density[gpu_is_track_interior] = estimate_position_density(
                    interior_place_bin_centers,
                    np.asarray(
                        position[is_spike & not_nan_position],
                        dtype=np.float32),
                    position_std,
                    block_size=block_size)

        summed_ground_process_intensity += (
            estimate_intensity(marginal_density, occupancy, mean_rates[-1]))

        is_mark_features = np.any(~np.isnan(multiunit), axis=0)
        encoding_marks.append(
            np.asarray(multiunit[
                np.ix_(is_spike & not_nan_position, is_mark_features)],
                dtype=np.float32))
        encoding_positions.append(position[is_spike & not_nan_position])

    summed_ground_process_intensity = summed_ground_process_intensity + \
        np.spacing(1)

    return {
        'encoding_marks': encoding_marks,
        'encoding_positions': encoding_positions,
        'summed_ground_process_intensity': summed_ground_process_intensity,
        'occupancy': occupancy,
        'mean_rates': mean_rates,
        'mark_std': mark_std,
        'position_std': position_std,
        'block_size': block_size,
        'bin_diffusion_distances': bin_diffusion_distances,
        'use_diffusion_distance': use_diffusion_distance,
        'edges': edges,
        **kwargs,
    }


def estimate_multiunit_likelihood2(multiunits,
                                   encoding_marks,
                                   mark_std,
                                   place_bin_centers,
                                   encoding_positions,
                                   position_std,
                                   occupancy,
                                   mean_rates,
                                   summed_ground_process_intensity,
                                   bin_diffusion_distances,
                                   edges,
                                   max_mark_value=6000,
                                   set_diag_zero=False,
                                   is_track_interior=None,
                                   time_bin_size=1,
                                   block_size=100,
                                   ignore_no_spike=False,
                                   disable_progress_bar=True,
                                   use_diffusion_distance=False):
    '''

    Parameters
    ----------
    multiunits : ndarray, shape (n_time, n_marks, n_electrodes)
    place_bin_centers : ndarray, (n_bins, n_position_dims)
    joint_pdf_models : list of sklearn models, shape (n_electrodes,)
    ground_process_intensities : list of ndarray, shape (n_electrodes,)
    occupancy : ndarray, (n_bins, n_position_dims)
    mean_rates : ndarray, (n_electrodes,)

    Returns
    -------
    log_likelihood : (n_time, n_bins)

    '''

    if is_track_interior is None:
        is_track_interior = np.ones((place_bin_centers.shape[0],),
                                    dtype=np.bool)
    else:
        is_track_interior = is_track_interior.ravel(order='F')

    n_time = multiunits.shape[0]
    if ignore_no_spike:
        log_likelihood = (-time_bin_size * summed_ground_process_intensity *
                          np.zeros((n_time, 1), dtype=np.float32))
    else:
        log_likelihood = (-time_bin_size * summed_ground_process_intensity *
                          np.ones((n_time, 1), dtype=np.float32))

    multiunits = np.moveaxis(multiunits, -1, 0)
    n_position_bins = is_track_interior.sum()
    interior_place_bin_centers = np.asarray(
        place_bin_centers[is_track_interior], dtype=np.float32)
    gpu_is_track_interior = np.asarray(is_track_interior)
    interior_occupancy = occupancy[gpu_is_track_interior]

    for multiunit, enc_marks, enc_pos, mean_rate in zip(
            tqdm(multiunits, desc='n_electrodes',
                 disable=disable_progress_bar),
            encoding_marks, encoding_positions, mean_rates):
        is_spike = np.any(~np.isnan(multiunit), axis=1)
        is_mark_features = np.any(~np.isnan(multiunit), axis=0)
        decoding_marks = np.asarray(
            multiunit[np.ix_(is_spike, is_mark_features)], dtype=np.float32)
        n_decoding_marks = decoding_marks.shape[0]
        log_joint_mark_intensity = np.zeros(
            (n_decoding_marks, n_position_bins), dtype=np.float32)

        if block_size is None:
            block_size = n_decoding_marks

        if use_diffusion_distance & (place_bin_centers.shape[1] > 1):
            position_distance = np.asarray(
                estimate_diffusion_position_distance(
                    enc_pos,
                    edges,
                    bin_distances=bin_diffusion_distances,
                )[:, is_track_interior], dtype=np.float32)
        else:
            position_distance = estimate_position_distance(
                interior_place_bin_centers,
                np.asarray(enc_pos, dtype=np.float32),
                position_std
            ).astype(np.float32)

        for start_ind in range(0, n_decoding_marks, block_size):
            block_inds = slice(start_ind, start_ind + block_size)
            log_joint_mark_intensity[block_inds] = estimate_log_joint_mark_intensity(
                decoding_marks[block_inds],
                enc_marks,
                mark_std,
                interior_occupancy,
                mean_rate,
                max_mark_value=max_mark_value,
                set_diag_zero=set_diag_zero,
                position_distance=position_distance,
            )
        log_likelihood[np.ix_(is_spike, is_track_interior)] += np.nan_to_num(
            log_joint_mark_intensity)

    log_likelihood[:, ~is_track_interior] = np.nan

    return log_likelihood
