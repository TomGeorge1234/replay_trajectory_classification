import math

import numba
import numpy as np
from sklearn.base import BaseEstimator, DensityMixin
from sklearn.decomposition import PCA
from sklearn.neighbors import KernelDensity

# Figure Parameters
MM_TO_INCHES = 1.0 / 25.4
ONE_COLUMN = 89.0 * MM_TO_INCHES
ONE_AND_HALF_COLUMN = 140.0 * MM_TO_INCHES
TWO_COLUMN = 178.0 * MM_TO_INCHES
PAGE_HEIGHT = 247.0 * MM_TO_INCHES
GOLDEN_RATIO = (np.sqrt(5) - 1.0) / 2.0
TRANSITION_TO_CATEGORY = {
    'identity': 'hover',
    'uniform': 'fragmented',
    'random_walk': 'continuous',
    'w_track_1D_random_walk': 'continuous',
}

PROBABILITY_THRESHOLD = 0.8

# Plotting Colors
STATE_COLORS = {
    'hover': '#9f043a',
    'fragmented': '#ff6944',
    'continuous': '#521b65',
    'hover-continuous-mix': '#61c5e6',
    'fragmented-continuous-mix': '#2a586a',
    '': '#c7c7c7',
}

SQRT_2PI = np.float64(math.sqrt(2.0 * math.pi))


class WhitenedKDE(BaseEstimator, DensityMixin):
    def __init__(self, **kwargs):
        self.kde = KernelDensity(**kwargs)
        self.pre_whiten = PCA(whiten=True)

    def fit(self, X, y=None, sample_weight=None):
        self.kde.fit(self.pre_whiten.fit_transform(X))
        return self

    def score_samples(self, X):
        return self.kde.score_samples(self.pre_whiten.transform(X))


@numba.njit(nogil=True, cache=True, parallel=True, error_model='numpy')
def numba_kde(eval_points, samples, bandwidths):
    '''

    Parameters
    ----------
    eval_points : np.ndarray, shape (n_eval_points, n_bandwidths)
    samples : np.ndarray, shape (n_samples, n_bandwidths)
    bandwidths : np.ndarray, shape (n_bandwidths,)


    Returns
    -------
    kernel_density_estimate : np.ndarray, shape (n_eval_points, n_samples)

    '''
    n_eval_points, n_bandwidths = eval_points.shape
    result = np.zeros((n_eval_points,))
    n_samples = len(samples)

    for i in numba.prange(n_eval_points):
        for j in range(n_samples):
            product_kernel = 1.0
            for k in range(n_bandwidths):
                bandwidth = bandwidths[k]
                eval_point = eval_points[i, k]
                sample = samples[j, k]
                product_kernel *= (np.exp(
                    -0.5 * ((eval_point - sample) / bandwidth)**2) /
                    (bandwidth * SQRT_2PI)) / bandwidth
            result[i] += product_kernel
        result[i] /= n_samples

    return result


class NumbaKDE(BaseEstimator, DensityMixin):
    def __init__(self, bandwidth=1.0):
        self.bandwidth = bandwidth

    def fit(self, X, y=None, sample_weight=None):
        self.training_data = X
        return self

    def score_samples(self, X):
        return np.log(numba_kde(X, self.training_data,
                                self.bandwidth[-X.shape[1]:]))


def get_2D_position(linear_position, classifier):
    linear_position = np.asarray(linear_position)
    original_shape = linear_position.shape
    linear_position = linear_position.ravel()
    actual_position_ind = np.searchsorted(
        classifier.place_bin_edges_.squeeze(), linear_position,
    )
    is_last_bin_edge = (
        actual_position_ind >= classifier.place_bin_centers_.size)
    actual_position_ind[is_last_bin_edge] = (
        classifier.place_bin_centers_.size - 1)

    # Handle linear position that falls into not track bins
    not_track_ind = np.nonzero(~classifier.is_track_interior_.squeeze())[0]
    is_not_track = np.isin(actual_position_ind, not_track_ind)
    actual_position_ind[is_not_track] = actual_position_ind[is_not_track] - 1

    return (classifier
            .place_bin_center_2D_position_[actual_position_ind]
            .reshape((*original_shape, 2)))
