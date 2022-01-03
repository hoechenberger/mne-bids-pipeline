"""

====================================================================
Decoding in time-frequency space using Common Spatial Patterns (CSP)
====================================================================

This file contains two main steps:
- 1. Decoding
The time-frequency decomposition is estimated by iterating over raw data that
has been band-passed at different frequencies. This is used to compute a
covariance matrix over each epoch or a rolling time-window and extract the CSP
filtered signals. A linear discriminant classifier is then applied to these
signals. More detail are available here:
https://mne.tools/stable/auto_tutorials/machine-learning/50_decoding.html#common-spatial-pattern
Warning: This step, especially the double loop on the time-frequency bins
is very computationally expensive.

- 2. Permutation statistics
We try to answer the following question: is the difference between
the two conditions statistically significant? We use the classic permutations
cluster tests on the time-frequency roc-auc map.
More details are available here:
https://mne.tools/stable/auto_tutorials/stats-sensor-space/10_background_stats.html

The user has only to specify the list of frequency and the list of timings.


Iterations levels:
- contrasts
    - We iterate through subjects and sessions
        - If there are multiple runs, runs are concatenated into one session.


Mathetically we do not seek here to create the best classifier,
and to optimize the rocauc score, we only seek
to obtain unbiased scores usable afterwards in the permutation test.
This is why it is not a big deal to optimize the running time
by making some approximations, such as:
- Decimation
- Selection of the mag channels, which contain a very large part of
    the information contained in (mag+grad)
- PCA, which is very useful to reduce the dimension for eeg
    where there is only one channel type.
"""
# License: BSD (3-clause)

import itertools
import os
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
import logging
from matplotlib.figure import Figure
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from scipy import stats

from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.decomposition import PCA

from mne.stats.cluster_level import permutation_cluster_1samp_test
from mne.epochs import BaseEpochs
from mne import create_info, read_epochs, compute_rank
from mne.decoding import UnsupervisedSpatialFilter, CSP
from mne.time_frequency import AverageTFR
from mne.parallel import parallel_func
from mne.utils import BunchConst
from mne.report import Report

from mne_bids import BIDSPath

from config import (N_JOBS, gen_log_kwargs, on_error,
                    failsafe_run)
from config import Tf, Pth, SessionT, ContrastT
import config

logger = logging.getLogger('mne-bids-pipeline')


# One PCA is fitted for each frequency bin.
# The usage of the pca is highly recommended for two reasons
# 1. The execution of the code is faster.
# 2. There will be much less numerical instabilities.
csp_use_pca = True

# ROC-AUC chance score level
chance = 0.5


def prepare_labels(*, epochs: BaseEpochs, contrast: ContrastT) -> np.ndarray:
    """Return the projection of the events_id on a boolean vector.

    This projection is useful in the case of hierarchical events:
    we project the different events contained in one condition into
    just one label.

    PARAMETERS:
    -----------
    epochs:
        Should only contain events contained in contrast.

    Returns:
    --------
    A boolean numpy array containing the labels.
    """
    epochs_cond_0 = epochs[contrast[0]]
    event_id_condition_0 = set(epochs_cond_0.events[:, 2])  # type: ignore
    epochs_cond_1 = epochs[contrast[1]]
    event_id_condition_1 = set(epochs_cond_1.events[:, 2])  # type: ignore

    y = epochs.events[:, 2].copy()
    for i in range(len(y)):
        if y[i] in event_id_condition_0 and y[i] in event_id_condition_1:
            msg = (f"Event_id {y[i]} is contained both in "
                   f"{contrast[0]}'s set {event_id_condition_0} and in "
                   f"{contrast[1]}'s set {event_id_condition_1}."
                   f"{contrast} does not constitute a valid partition.")
            logger.critical(msg)
        elif y[i] in event_id_condition_0:
            y[i] = 0
        elif y[i] in event_id_condition_1:
            y[i] = 1
        else:
            # This should not happen because epochs should already by filtered
            msg = (f"Event_id {y[i]} is not contained in "
                   f"{contrast[0]}'s set {event_id_condition_0}  nor in "
                   f"{contrast[1]}'s set {event_id_condition_1}.")
            logger.critical(msg)
    return y


def prepare_epochs_and_y(
    *,
    epochs: BaseEpochs,
    contrast: ContrastT,
    cfg,
    fmin: float,
    fmax: float
) -> Tuple[BaseEpochs, np.ndarray]:
    """Band-pass between (fmin, fmax), clean the epochs, prepare labels.

    Returns:
    --------
    epochs_filter, y
    """
    epochs_filter = epochs.copy()

    epochs_filter.pick_types(
        meg=True, eeg=True, stim=False, eog=False,
        exclude='bads')

    # We only take mag to speed up computation
    # because the information is redundant between grad and mag
    if cfg.datatype == "meg":
        epochs_filter.pick_types(meg="mag")

    # filtering out the conditions we are not interested in.
    # So we ensure here we have a valid partition between the
    # condition of the contrast.
    epochs_filter = epochs_filter[contrast]

    # We frequency filter after droping channel,
    # because filtering is costly
    epochs_filter = epochs_filter.filter(fmin, fmax, n_jobs=1)
    y = prepare_labels(epochs=epochs_filter, contrast=contrast)

    return epochs_filter, y


def plot_frequency_decoding(
    *,
    freqs: np.ndarray,
    freq_scores: np.ndarray,
    conf_int: np.ndarray,
    subject: str
) -> Figure:
    """Plot and save the frequencies results.

    Show and save the roc-auc score in a 1D histogram for
    each frequency bin.

    Keep in mind that the confidence intervals indicated on the individual
    plot use only the std of the cross-validation scores.

    Parameters:
    -----------
    freqs
        The frequencies bins.
    freq_scores
        The roc-auc scores for each frequency bin.
    freq_scores_std
        The std of the cross-validation roc-auc scores for each frequency bin.
    subject
        name of the subject or "average" subject

    Returns:
    -------
    Histogram with frequency bins.
    For the average subject, we also plot the std.
    """
    fig, ax = plt.subplots()

    yerr = conf_int if len(subject) > 1 else None

    ax.bar(x=freqs[:-1], height=freq_scores, yerr=yerr,
           width=np.diff(freqs)[0],
           align='edge', edgecolor='black')

    # fixing the overlapping labels
    round_label = np.around(freqs)
    round_label = round_label.astype(int)
    ax.set_xticks(ticks=freqs)
    ax.set_xticklabels(round_label)

    ax.set_ylim([0, 1])

    ax.axhline(chance,
               color='k', linestyle='--',
               label='chance level')
    ax.legend()
    ax.set_xlabel('Frequency (Hz)')
    ax.set_ylabel('Decoding Roc-Auc Scores')
    CI_msg = "95% CI" if subject == "average" else "CV std score"
    ax.set_title(f'Frequency Decoding Roc-Auc Scores - {CI_msg}')
    return fig


def plot_time_frequency_decoding(
    *,
    tf_scores: np.ndarray,
    tf: Tf,
    subject: str
) -> Figure:
    """Plot and save the time-frequencies results.

    Parameters:
    -----------
    tf_scores
        The roc-auc scores for each time-frequency bin.
    sfreq
        Sampling frequency
    tf
        Util object for time frequency information.
    subject
        name of the subject.

    Returns:
    -------
    The roc-auc score in a 2D map for each time-frequency bin.
    """
    if np.isnan(tf_scores).any():
        msg = ("There is at least one nan value in one of "
               "the time-frequencies bins.")
        logger.info(**gen_log_kwargs(message=msg,
                                     subject=subject))
    tf_scores_ = np.nan_to_num(tf_scores, nan=chance)

    # Here we just use an sfreq random number like 1. Hz just
    # to create a basic info object.
    av_tfr = AverageTFR(
        info=create_info(['freq'], sfreq=1.),
        # newaxis linked with the [0] in plot
        data=tf_scores_[np.newaxis, :],
        times=tf.centered_w_times,
        freqs=tf.centered_w_freqs,
        nave=1
    )

    # Centered color map around the chance level.
    max_abs_v = np.max(np.abs(tf_scores_ - chance))
    figs = av_tfr.plot(
        [0],  # [0] We do not have multiple channels here.
        vmax=chance + max_abs_v,
        vmin=chance - max_abs_v,
        title="Time-Frequency Decoding ROC-AUC Scores",
    )
    return figs[0]


def plot_patterns(
    *,
    csp,
    epochs_filter: BaseEpochs,
    report: Report,
    section: str,
    title: str
):
    """Plot csp topographic patterns and save them in the reports.

    PARAMETERS
    ----------
    csp
        csp fitted estimator.
    epochs_filter
        Epochs which have been band passed filtered and maybe time cropped.
    report
        Where to save the topographic plot.
    section
        choose the section of the report.
    title
        Title of the figure in the report.

    RETURNS
    -------
    None. Just save the figure in the report.
    """
    fig = csp.plot_patterns(epochs_filter.info)
    report.add_figure(
        fig=fig,
        title=f'{section}: {title}',
        tags=('csp',)
    )


@failsafe_run(on_error=on_error, script_path=__file__)
def one_subject_decoding(
    *,
    cfg,
    tf: Tf,
    pth: Pth,
    subject: str,
    session: SessionT,
    contrast: ContrastT
) -> None:
    """Run one subject.

    There are two steps in this function:
    1. The frequency analysis.
    2. The time-frequency analysis.

    For each bin of those plot, we train a classifier to discriminate
    the two conditions.
    Then, we plot the roc-auc of the classifier.

    Returns
    -------
    None. We just save the plots in the report
    and the numpy results in memory.
    """
    # sub_ses_con = pth.prefix(subject, session, contrast)
    msg = f"Running decoding ..."
    logger.info(**gen_log_kwargs(msg, subject=subject, session=session))

    if not config.interactive:
        matplotlib.use('Agg')

    report = Report(title=f"csp-permutations-sub-{subject}")

    epochs = read_epochs(pth.file(subject=subject, session=session))
    tf.check_csp_times(epochs)

    # Compute maximal decimation possible
    # 3 is to take a bit of margin wrt Nyquist
    decimation_needed = epochs.info["sfreq"] / (3*tf.freqs[-1])
    if decimation_needed > 2 and cfg.csp_quick:
        epochs.decimate(int(decimation_needed))
        msg = f"Decimating by a factor {int(decimation_needed)}"
        logger.info(**gen_log_kwargs(msg, subject=subject))

    # Chosing the right rank:
    # 1. Selecting the channel group with the smallest rank (Usefull for meg)
    rank_dic = compute_rank(epochs, rank="info")
    ch_type = min(rank_dic, key=rank_dic.get)  # type: ignore
    rank = rank_dic[ch_type]
    # 2. If there is no channel type, we reduce the dimension
    # to a reasonable number. (Useful for eeg)
    if rank > 100:
        msg = ("Manually reducing the dimension to 100.")
        logger.info(**gen_log_kwargs(msg, subject=subject, session=session))
        rank = 100
    pca = UnsupervisedSpatialFilter(PCA(rank), average=False)
    msg = f"Reducing data dimension via PCA; new rank: {rank}."
    logger.info(**gen_log_kwargs(msg, subject=subject, session=session))

    # Classifier
    csp = CSP(
        n_components=cfg.csp_n_components,
        reg=cfg.csp_reg,
        log=True,
        norm_trace=False
    )
    clf = make_pipeline(csp, LinearDiscriminantAnalysis())
    random_state = None if cfg.csp_shuffle_cv is False else cfg.random_state 
    cv = StratifiedKFold(
        n_splits=cfg.decoding_n_splits,
        shuffle=cfg.csp_shuffle_cv,
        random_state=random_state
    )

    freq_scores = np.zeros((tf.n_freq_windows,))
    freq_scores_std = np.zeros((tf.n_freq_windows,))
    tf_scores = np.zeros((tf.n_freq_windows, tf.n_time_windows))

    for freq, (fmin, fmax) in enumerate(tf.freq_ranges):

        epochs_filter, y = prepare_epochs_and_y(
            epochs=epochs, contrast=contrast, fmin=fmin, fmax=fmax, cfg=cfg
        )

        ######################################################################
        # 1. Loop through frequencies, apply classifier and save scores

        X = epochs_filter.get_data()
        X_pca = pca.fit_transform(X) if csp_use_pca else X
        cv_scores = cross_val_score(estimator=clf, X=X_pca, y=y,
                                    scoring='roc_auc', cv=cv,
                                    n_jobs=1)

        freq_scores_std[freq] = np.std(cv_scores, axis=0)
        freq_scores[freq] = np.mean(cv_scores, axis=0)

        # Plot patterns. We cannot use pca if plotting the pattern, as we'll
        # need channel locations.
        title = f'sub-{subject}'
        if session:
            title += ', ses-{session}, '
        title += f'{contrast}, {fmin}–{fmax} Hz, full epochs'
        
        # csp.fit(X, y)
        # plot_patterns(
        #     csp=csp,
        #     epochs_filter=epochs_filter,
        #     report=report,
        #     section="CSP Patterns - frequency",
        #     title=title
        # )

        ######################################################################
        # 2. Loop through frequencies and time

        # Roll covariance, csp and lda over time
        for t, (w_tmin, w_tmax) in enumerate(tf.time_ranges):
            msg = (f'Contrast: {contrast[0]} – {contrast[1]}, '
                   f'Freqs (Hz): {fmin}–{fmax}, '
                   f'Times (sec): {w_tmin}–{w_tmax}')
            logger.info(
                **gen_log_kwargs(msg, subject=subject, session=session)
            )

            # Originally the window size varied accross frequencies...
            # But this means also that there is some mutual information between
            # 2 different pixels in the map.
            # So the simple way to deal with this is just to fix
            # the windows size for all frequencies.

            # Crop data into time window of interest
            X = epochs_filter.copy().crop(w_tmin, w_tmax).get_data()
            # TODO transform or fit_transform?
            X_pca = pca.transform(X) if csp_use_pca else X

            cv_scores = cross_val_score(estimator=clf,
                                        X=X_pca, y=y,
                                        scoring='roc_auc',
                                        cv=cv,
                                        n_jobs=1)
            tf_scores[freq, t] = np.mean(cv_scores, axis=0)

            # We plot the patterns using all the epochs without splitting the
            # epochs by using CV.
            # We cannot use pca if plotting the pattern, as we'll need channel
            # locations.
            csp.fit(X, y)
            title = f'sub-{subject}'
            if session:
                title += ', ses-{session}, '
            title += f'{contrast}, {fmin}–{fmax} Hz, {w_tmin}–{w_tmax} s'
            # plot_patterns(
            #     csp=csp, epochs_filter=epochs_filter, report=report,
            #     section="CSP Patterns - time-frequency",
            #     title=title
            # )

    # Frequency savings
    np.save(file=pth.freq_scores(subject, session, contrast), arr=freq_scores)
    np.save(file=pth.freq_scores_std(
        subject, session, contrast), arr=freq_scores_std)

    fig = plot_frequency_decoding(
        freqs=tf.freqs,
        freq_scores=freq_scores,
        conf_int=freq_scores_std,
        subject=subject)

    title = "Frequency roc-auc decoding"
    if session:
        title += ', ses-{session}, '
    title += f'{contrast}'
    report.add_figure(
        fig=fig,
        title=title,
        tags=('csp',)
    )

    # Time frequency savings
    np.save(file=pth.tf_scores(subject, session, contrast), arr=tf_scores)
    fig = plot_time_frequency_decoding(
        tf=tf, tf_scores=tf_scores, subject=subject)
    title = "Time-frequency decoding"
    if session:
        title += ', ses-{session}, '
    title += f'{contrast}'
    report.add_figure(
        fig=fig,
        title=title,
        tags=('csp',)
    )
    report.save(pth.report(subject, session, contrast), overwrite=True,
                open_browser=config.interactive)


def load_and_average(
    path: Callable[[str, SessionT, ContrastT], BIDSPath],
    subjects: List[str],
    contrast: ContrastT,
    shape: List[int],
    average: bool = True
) -> np.ndarray:
    """Load and average a np.array.

    We average between all subjects and all sessions.

    Parameters:
    -----------
    path
        function of the subject, returning the path of the numpy array.
    average
        if True, returns average along the subject dimension.
    shape
        The shape of the results.
        Either (freq) or (freq, times)

    Returns:
    --------
    The loaded array.

    Warning:
    --------
    Gives the list of files containing NaN values.
    """
    sessions = config.get_sessions()
    len_session = len(sessions) if type(sessions) == list else 1
    shape_all = [len(subjects) * len_session] + list(shape)
    res = np.zeros(shape_all)
    iterator = itertools.product(subjects, sessions)
    for i, (sub, ses) in enumerate(iterator):
        try:
            arr = np.load(path(sub, ses, contrast))
            # Checking for previous iteration, previous shapes
            if list(arr.shape) != shape:
                msg = f"Shape mismatch for {path(sub, ses, contrast)}"
                logger.warning(**gen_log_kwargs(
                    message=msg, subject=sub))
                raise FileNotFoundError
        except FileNotFoundError:
            arr = np.empty(shape=shape)
            arr.fill(np.NaN)
        if np.isnan(arr).any():
            msg = f"NaN values were found in {path(sub, ses, contrast)}"
            logger.warning(**gen_log_kwargs(
                message=msg, subject=sub, session=ses))
        res[i] = arr
    if average:
        return np.nanmean(res, axis=0)
    else:
        return res


def plot_axis_time_frequency_statistics(
    *,
    ax: plt.Axes,
    array: np.ndarray,
    value_type: Literal['p', 't'],
    tf: Tf
) -> None:
    """Plot one 2D axis containing decoding statistics.

    Parameters:
    -----------
    ax :
        inplace plot in this axis.
    array :
        The two dimensianal array containing the results.
    value_type :
        either "p" or "t" values.

    Returns:
    --------
    None. inplace modification of the axis.
    """
    ax.set_title(f"{value_type}-value")
    array = np.maximum(array, 1e-7) if value_type == "p" else array
    array = np.reshape(array, (tf.n_freq_windows, tf.n_time_windows))
    array = -np.log10(array) if value_type == "p" else array

    # Adaptive color
    lims = np.array([np.min(array), np.max(array)])

    img = ax.imshow(array, cmap='Reds', origin='lower',
                    vmin=lims[0], vmax=lims[1], aspect='auto',
                    extent=[np.min(tf.times), np.max(tf.times),
                            np.min(tf.freqs), np.max(tf.freqs)])

    ax.set_xlabel('time')
    ax.set_ylabel('frequencies')
    cbar = plt.colorbar(ax=ax, shrink=0.75, orientation='horizontal',
                        mappable=img, )
    cbar.set_ticks(lims)

    cbar.set_ticklabels([f'10$^{{{-round(lim, 1)}}}$' for lim in lims])
    cbar.ax.get_xaxis().set_label_coords(0.5, -0.3)
    if value_type == "p":
        cbar.set_label(r'p-value')
    if value_type == "t":
        cbar.set_label(r't-value')


def plot_t_and_p_values(
    *,
    t_values: np.ndarray,
    p_values: np.ndarray,
    title: str,
    tf: Tf
) -> Figure:
    """Plot t-values and either (p-values or clusters).

    Returns
    -------
    A figure with two subplot: t-values and p-values.
    """
    fig = plt.figure(figsize=(10, 5))
    axes = [fig.add_subplot(121), fig.add_subplot(122)]

    plot_axis_time_frequency_statistics(
        ax=axes[0], array=t_values, value_type="t", tf=tf)
    plot_axis_time_frequency_statistics(
        ax=axes[1], array=p_values, value_type="p", tf=tf)
    plt.tight_layout()
    fig.suptitle(title)
    return fig


def compute_conf_inter(
    mean_scores: np.ndarray,
    subjects: List[str],
    contrast: ContrastT,
    cfg,
    tf: Tf
) -> Dict[str, Any]:
    """Compute the 95% confidence interval through bootstrapping.

    For the moment only serves for frequency histogram.

    TODO : copy pasted from https://github.com/mne-tools/mne-bids-pipeline/blob/main/scripts/sensor/04-group_average.py#L158  # noqa: E501
    Maybe we could create a common function in mne?

    PARAMETERS:
    -----------
    mean_scores: np.array((len(subjects)*nb_session, len(times)))

    RETURNS:
    --------
    Dictionnary of meta data.
    """
    contrast_score_stats = {
        'cond_1': contrast[0],
        'cond_2': contrast[1],
        'times': tf.times,
        'N': len(subjects),
        'mean': np.empty(tf.n_freq_windows),
        'mean_min': np.empty(tf.n_freq_windows),
        'mean_max': np.empty(tf.n_freq_windows),
        'mean_se': np.empty(tf.n_freq_windows),
        'mean_ci_lower': np.empty(tf.n_freq_windows),
        'mean_ci_upper': np.empty(tf.n_freq_windows)}

    # Now we can calculate some descriptive statistics on the mean scores.
    # We use the [:] here as a safeguard to ensure we don't mess up the
    # dimensions.
    contrast_score_stats['mean'][:] = np.nanmean(mean_scores, axis=0)
    contrast_score_stats['mean_min'][:] = mean_scores.min(axis=0)
    contrast_score_stats['mean_max'][:] = mean_scores.max(axis=0)

    # Finally, for each time point, bootstrap the mean, and calculate the
    # SD of the bootstrapped distribution: this is the standard error of
    # the mean. We also derive 95% confidence intervals.
    rng = np.random.default_rng(seed=cfg.random_state)

    for time_idx in range(tf.n_freq_windows):
        scores_resampled = rng.choice(mean_scores[:, time_idx],
                                      size=(cfg.n_boot, len(subjects)),
                                      replace=True)
        bootstrapped_means = scores_resampled.mean(axis=1)

        # SD of the bootstrapped distribution == SE of the metric.
        se = bootstrapped_means.std(ddof=1)
        ci_lower = np.quantile(bootstrapped_means, q=0.025)
        ci_upper = np.quantile(bootstrapped_means, q=0.975)

        contrast_score_stats['mean_se'][time_idx] = se
        contrast_score_stats['mean_ci_lower'][time_idx] = ci_lower
        contrast_score_stats['mean_ci_upper'][time_idx] = ci_upper

        del bootstrapped_means, se, ci_lower, ci_upper

    # We cannot use the logger here
    print("Confidence intervals results:")
    print(mean_scores)

    return contrast_score_stats


@failsafe_run(on_error=on_error, script_path=__file__)
def group_analysis(
    subjects: List[str],
    contrast: ContrastT,
    cfg,
    pth: Pth,
    tf: Tf
) -> None:
    """Group analysis.

    1. Average roc-auc scores:
        - frequency 1D histogram
        - time-frequency 2D color-map
    2. Perform statistical tests
        - plot t-values and p-values
        - performs classic cluster permutation test

    Returns
    -------
    None. Plots are saved in memory.
    """
    msg = "Running group analysis..."
    logger.info(**gen_log_kwargs(msg))

    if len(subjects) < 2:
        msg = "We cannot run a group analysis with just one subject."
        logger.warning(**gen_log_kwargs(message=msg))
        return None

    report = Report(title="csp-permutations-sub-average")

    ######################################################################
    # 1. Average roc-auc scores across subjects

    # Average Frequency analysis
    all_freq_scores = load_and_average(
        pth.freq_scores, subjects=subjects, contrast=contrast,
        average=False, shape=[tf.n_freq_windows])
    freq_scores = np.nanmean(all_freq_scores, axis=0)

    # Calculating the 95% confidence intervals
    contrast_score_stats = compute_conf_inter(
        mean_scores=all_freq_scores, contrast=contrast,
        subjects=subjects, cfg=cfg, tf=tf)

    fig = plot_frequency_decoding(
        freqs=tf.freqs, freq_scores=freq_scores,
        conf_int=contrast_score_stats["mean_se"],
        subject="average")
    section = "Frequency decoding"
    report.add_figure(
        fig=fig,
        title=section + ' sub-average',
        tags=('csp',)
    )

    # Average time-Frequency analysis
    all_tf_scores = load_and_average(
        pth.tf_scores, subjects=subjects, contrast=contrast,
        shape=[tf.n_freq_windows, tf.n_time_windows])

    fig = plot_time_frequency_decoding(
        tf=tf, tf_scores=all_tf_scores, subject="average")
    section = "Time - frequency decoding"
    report.add_figure(
        fig=fig,
        title=section + ' sub-average',
        tags=('csp',)
    )

    ######################################################################
    # 2. Statistical tests

    # Reshape data to what is equivalent to (n_samples, n_space, n_time)
    X = load_and_average(
        pth.tf_scores, subjects=subjects, average=False, contrast=contrast,
        shape=[tf.n_freq_windows, tf.n_time_windows])
    X = X - chance

    # Analyse with cluster permutation statistics
    titles = ['Without clustering']
    out = stats.ttest_1samp(X, 0, axis=0)
    ts: List[np.ndarray] = [np.array(out[0])]  # statistics
    ps: List[np.ndarray] = [np.array(out[1])]  # pvalues

    mccs = [False]  # these are not multiple-comparisons corrected

    titles.append('Clustering')
    # Compute threshold from t distribution (this is also the default)
    threshold = stats.distributions.t.ppf(  # type: ignore
        1 - cfg.cluster_t_dist_alpha_thres, len(subjects) - 1)
    t_clust, clusters, p_values, H0 = permutation_cluster_1samp_test(
        X, n_jobs=1,
        threshold=threshold,
        adjacency=None,  # a regular lattice adjacency is assumed
        n_permutations=cfg.n_permutations, out_type='mask')

    msg = "Permutations performed successfully"
    logger.info(**gen_log_kwargs(msg))

    # Put the cluster data in a viewable format
    p_clust = np.ones((tf.n_freq_windows, tf.n_time_windows))
    for cl, p in zip(clusters, p_values):
        p_clust[cl] = p
    msg = (f"We found {len(p_values)} clusters "
           f"each one with a p-value of {p_values}.")
    logger.info(**gen_log_kwargs(msg))

    if len(p_values) == 0 or np.min(p_values) > cfg.cluster_stats_alpha:
        msg = ("The results are not significant. "
               "Try increasing the number of subjects.")
        logger.info(**gen_log_kwargs(msg))
    else:
        msg = (f"Congrats, the results seem significant. At least one of "
               f"your cluster has a significant p-value "
               f"at the level {cfg.cluster_stats_alpha}. "
               "This means that there is probably a meaningful difference "
               "between the two states, highlighted by the difference in "
               "cluster size.")
        logger.info(**gen_log_kwargs(msg))

    ts.append(t_clust)
    ps.append(p_clust)
    mccs.append(True)
    for i in range(2):
        fig = plot_t_and_p_values(
            t_values=ts[i], p_values=ps[i], title=titles[i], tf=tf)

        cluster = "with" if i else "without"
        section = f"Time - frequency statistics - {cluster} cluster"
        report.add_figure(
            fig=fig,
            title=section + ' sub-average',
            tags=('csp',)
        )

    pth_report = pth.report("average", session=None, contrast=contrast)
    if not pth_report.fpath.parent.exists():
        os.makedirs(pth_report.fpath.parent)
    report.save(pth_report, overwrite=True,
                open_browser=config.interactive)
    msg = f"CSP final Report {pth_report} saved in the average subject folder"
    logger.info(**gen_log_kwargs(message=msg))

    msg = "Group statistic analysis finished."
    logger.info(**gen_log_kwargs(msg))


def get_config(
    subject: Optional[str] = None,
    session: Optional[str] = None
) -> BunchConst:
    cfg = BunchConst(
        # Data parameters
        datatype=config.get_datatype(),
        deriv_root=config.get_deriv_root(),
        task=config.get_task(),
        acq=config.acq,
        rec=config.rec,
        space=config.space,
        # Processing parameters
        csp_quick=config.csp_quick,
        csp_freqs=config.csp_freqs,
        csp_times=config.csp_times,
        decoding_n_splits=config.decoding_n_splits,
        csp_n_components=config.csp_n_components,
        csp_reg=config.csp_reg,
        csp_shuffle_cv=config.csp_shuffle_cv,
        cluster_stats_alpha=config.cluster_stats_alpha,
        cluster_t_dist_alpha_thres=config.cluster_t_dist_alpha_thres,
        n_boot=config.n_boot,
        n_permutations=config.n_permutations,
        random_state=config.random_state,
        interactive=config.interactive,
    )
    return cfg


def main():
    """Run all subjects decoding in parallel."""
    msg = 'Running Step 3b: CSP'
    logger.info(**gen_log_kwargs(message=msg))

    cfg = get_config()

    if not config.contrasts:
        msg = ('contrasts was not specified. '
               'Skipping step CSP ...')
        logger.info(**gen_log_kwargs(message=msg))
        return None

    # Calculate the appropriate time and frequency windows size
    tf = Tf(freqs=config.csp_freqs, times=config.csp_times)

    # Compute the paths
    pth = Pth(cfg=cfg)

    subjects = config.get_subjects()
    sessions = config.get_sessions()

    for contrast in config.contrasts:

        parallel, run_func, _ = parallel_func(
            one_subject_decoding, n_jobs=N_JOBS)
        logs = parallel(
            run_func(cfg=cfg, tf=tf, pth=pth,
                     subject=subject,
                     session=session,
                     contrast=contrast)
            for subject, session in
            itertools.product(subjects, sessions)
        )
        config.save_logs(logs)

        # Once every subject has been calculated,
        # the group_analysis is very fast to compute.
        group_analysis(subjects=subjects,
                       contrast=contrast,
                       cfg=cfg, pth=pth, tf=tf)

    msg = 'Completed Step 8: Time-frequency decoding'
    logger.info(**gen_log_kwargs(message=msg))


if __name__ == '__main__':
    main()
