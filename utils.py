
""" Copyright 2016-2017 ETH Zurich, Eirini Arvaniti and Manfred Claassen.

This module contains utility functions.

"""

import os
import errno
from collections import Counter
import numpy as np
import pandas as pd
import copy
from cellCnn.downsample import random_subsample, kmeans_subsample, outlier_subsample
from cellCnn.downsample import weighted_subsample
import sklearn.utils as sku
from sklearn.metrics.pairwise import pairwise_kernels, pairwise_distances
from scipy.cluster.hierarchy import linkage
from scipy.cluster.hierarchy import fcluster
from scipy import stats
from scipy.sparse import coo_matrix
import flowio
try:
    import igraph
except ImportError:
    pass


# extra arguments accepted for backwards-compatibility (with the fcm-0.9.1 package)
def loadFCS(filename, *args, **kwargs):
    f = flowio.FlowData(filename)
    events = np.reshape(f.events, (-1, f.channel_count))
    channels = []
    for i in range(1, f.channel_count+1):
        key = str(i)
        if 'PnS' in f.channels[key] and f.channels[key]['PnS'] != u' ':
            channels.append(f.channels[key]['PnS'])
        elif 'PnN' in f.channels[key] and f.channels[key]['PnN'] != u' ':
            channels.append(f.channels[key]['PnN'])
        else:
            channels.append('None')
    return FcmData(events, channels)

class FcmData(object):
    def __init__(self, events, channels):
        self.channels = channels
        self.events = events
        self.shape = events.shape

    def __array__(self):
        return self.events


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

def get_data(indir, info, marker_names, do_arcsinh, cofactor):
    fnames, phenotypes = info[:, 0], info[:, 1]
    sample_list = []
    for fname in fnames:
        full_path = os.path.join(indir, fname)
        fcs = loadFCS(full_path, transform=None, auto_comp=False)
        marker_idx = [fcs.channels.index(name) for name in marker_names]
        x = np.asarray(fcs)[:, marker_idx]
        if do_arcsinh:
            x = ftrans(x, cofactor)
        sample_list.append(x)
    return sample_list, list(phenotypes)

def save_results(results, outdir, labels):
    csv_dir = os.path.join(outdir, 'exported_filter_weights')
    mkdir_p(csv_dir)
    nmark = len(labels)
    nc = results['w_best_net'].shape[1] - (nmark+1)
    labels_ = labels + ['constant'] + ['out %d' % i for i in range(nc)]
    w = pd.DataFrame(results['w_best_net'], columns=labels_)
    w.to_csv(os.path.join(csv_dir, 'filters_best_net.csv'), index=False)
    w = pd.DataFrame(results['selected_filters'], columns=labels_)
    w.to_csv(os.path.join(csv_dir, 'filters_consensus.csv'), index=False)
    w = pd.DataFrame(results['clustering_result']['w'], columns=labels_)
    w.to_csv(os.path.join(csv_dir, 'filters_all.csv'), index=False)

def get_items(l, idx):
    return [l[i] for i in idx]

def get_immediate_subdirectories(a_dir):
    return [name for name in os.listdir(a_dir)
            if os.path.isdir(os.path.join(a_dir, name))]

def ftrans(x, c):
    return np.arcsinh(1./c * x)

def rectify(X):
    return np.max(np.hstack([X.reshape(-1, 1), np.zeros((X.shape[0], 1))]), axis=1)

def relu(x):
    return x * (x > 0)

def combine_samples(data_list, sample_id):
    accum_x, accum_y = [], []
    for x, y in zip(data_list, sample_id):
        accum_x.append(x)
        accum_y.append(y * np.ones(x.shape[0], dtype=int))
    return np.vstack(accum_x), np.hstack(accum_y)

def keras_param_vector(params):
    W = np.squeeze(params[0])
    b = params[1]
    W_out = params[2]
    # store the (convolutional weights + biases + output weights) per filter
    W_tot = np.hstack([W, b.reshape(-1, 1), W_out])
    return W_tot

def representative(data, metric='cosine', stop=None):
    if stop is None:
        i = np.argmax(np.sum(pairwise_kernels(data, metric=metric), axis=1))
    else:
        i = np.argmax(np.sum(pairwise_kernels(data[:, :stop], metric=metric), axis=1))
    return data[i]

def cluster_tightness(data, metric='cosine'):
    centroid = np.mean(data, axis=0).reshape(1, -1)
    return np.mean(pairwise_kernels(data, centroid, metric=metric))

def cluster_profiles(param_dict, nmark, accuracies, accur_thres=.99,
                     dendrogram_cutoff=.5):
    accum = []
    # if not at least 3 models reach the accuracy threshold, select the filters from the 3 best
    if np.sort(accuracies)[-3] < accur_thres:
        accur_thres = np.sort(accuracies)[-3]

    # combine filters from multiple models
    for i, params in param_dict.items():
        if accuracies[i] >= accur_thres:
            W_tot = keras_param_vector(params)
            accum.append(W_tot)
    w_strong = np.vstack(accum)

    # perform hierarchical clustering on cosine distances
    Z = linkage(w_strong[:, :nmark+1], 'average', metric='cosine')
    clusters = fcluster(Z, dendrogram_cutoff, criterion='distance') - 1
    c = Counter(clusters)
    cons = []
    for key, val in c.items():
        if val > 1:
            members = w_strong[clusters == key]
            cons.append(representative(members, stop=nmark+1))
    if cons != []:
        cons_profile = np.vstack(cons)
    else:
        cons_profile = None
    cl_res = {'w': w_strong, 'cluster_linkage': Z, 'cluster_assignments': clusters}
    return cons_profile, cl_res

def normalize_outliers(X, lq=.5, hq=99.5, stop=None):
    if stop is None:
        stop = X.shape[1]
    for jj in range(stop):
        marker_t = X[:, jj]
        low, high = np.percentile(marker_t, lq), np.percentile(marker_t, hq)
        X[marker_t < low, jj] = low
        X[marker_t > high, jj] = high
    return X

def normalize_outliers_to_control(ctrl_list, list2, lq=.5, hq=99.5, stop=None):
    X = np.vstack(ctrl_list)
    accum = []
    if stop is None:
        stop = X.shape[1]

    for xx in ctrl_list + list2:
        for jj in range(stop):
            marker_ctrl = X[:, jj]
            low, high = np.percentile(marker_ctrl, lq), np.percentile(marker_ctrl, hq)
            marker_t = xx[:, jj]
            xx[marker_t < low, jj] = low
            xx[marker_t > high, jj] = high
        accum.append(xx)
    return accum

## Utilities for generating random subsets ##

def filter_per_class(X, y, ylabel):
    return X[np.where(y == ylabel)]

def per_sample_subsets(X, nsubsets, ncell_per_subset, k_init=False):
    nmark = X.shape[1]
    shape = (nsubsets, nmark, ncell_per_subset)
    Xres = np.zeros(shape)

    if not k_init:
        for i in range(nsubsets):
            X_i = random_subsample(X, ncell_per_subset)
            Xres[i] = X_i.T
    else:
        for i in range(nsubsets):
            X_i = random_subsample(X, 2000)
            X_i = kmeans_subsample(X_i, ncell_per_subset, random_state=i)
            Xres[i] = X_i.T
    return Xres

def generate_subsets(X, pheno_map, sample_id, nsubsets, ncell,
                     per_sample=False, k_init=False):
    S = dict()
    n_out = len(np.unique(sample_id))

    for ylabel in range(n_out):
        X_i = filter_per_class(X, sample_id, ylabel)
        if per_sample:
            S[ylabel] = per_sample_subsets(X_i, nsubsets, ncell, k_init)
        else:
            n = nsubsets[pheno_map[ylabel]]
            S[ylabel] = per_sample_subsets(X_i, n, ncell, k_init)

    # mix them
    data_list, y_list = [], []
    for y_i, x_i in S.items():
        data_list.append(x_i)
        y_list.append(pheno_map[y_i] * np.ones(x_i.shape[0], dtype=int))

    Xt = np.vstack(data_list)
    yt = np.hstack(y_list)
    Xt, yt = sku.shuffle(Xt, yt)
    return Xt, yt

def per_sample_biased_subsets(X, x_ctrl, nsubsets, ncell_final, to_keep, ratio_biased):
    nmark = X.shape[1]
    Xres = np.empty((nsubsets, nmark, ncell_final))
    nc_biased = int(ratio_biased * ncell_final)
    nc_unbiased = ncell_final - nc_biased

    for i in range(nsubsets):
        print i
        x_unbiased = random_subsample(X, nc_unbiased)
        if (i % 100) == 0:
            x_outlier, outlierness = outlier_subsample(X, x_ctrl, to_keep)
        x_biased = weighted_subsample(x_outlier, outlierness, nc_biased)
        Xres[i] = np.vstack([x_biased, x_unbiased]).T
    return Xres

def generate_biased_subsets(X, pheno_map, sample_id, x_ctrl, nsubset_ctrl, nsubset_biased,
                            ncell_final, to_keep, id_ctrl, id_biased):
    S = dict()
    for ylabel in id_biased:
        X_i = filter_per_class(X, sample_id, ylabel)
        n = nsubset_biased[pheno_map[ylabel]]
        S[ylabel] = per_sample_biased_subsets(X_i, x_ctrl, n,
                                              ncell_final, to_keep, 0.5)
    for ylabel in id_ctrl:
        X_i = filter_per_class(X, sample_id, ylabel)
        S[ylabel] = per_sample_subsets(X_i, nsubset_ctrl, ncell_final, k_init=False)

    # mix them
    data_list, y_list = [], []
    for y_i, x_i in S.items():
        data_list.append(x_i)
        y_list.append(pheno_map[y_i] * np.ones(x_i.shape[0], dtype=int))
    Xt = np.vstack(data_list)
    yt = np.hstack(y_list)
    Xt, yt = sku.shuffle(Xt, yt)
    return Xt, yt

def single_filter_output(filter_params, valid_samples, mp):
    y_pred = np.zeros(len(valid_samples))
    nmark = valid_samples[0].shape[1]
    w, b = filter_params[:nmark], filter_params[nmark]
    w_out = filter_params[nmark+1:]

    for i, x in enumerate(valid_samples):
        g = relu(np.sum(w.reshape(1, -1) * x, axis=1) + b)
        ntop = max(1, int(mp/100. * x.shape[0]))
        gpool = np.mean(np.sort(g)[-ntop:])
        y_pred[i] = gpool
    return y_pred, np.argmax(w_out)


def get_filters_classification(filters, scaler, valid_samples, valid_phenotypes, mp):
    y_true = np.array(valid_phenotypes)
    filter_diff = np.zeros(len(filters))

    if scaler is not None:
        valid_samples = copy.deepcopy(valid_samples)
        valid_samples = [scaler.transform(x) for x in valid_samples]

    for i, filter_params in enumerate(filters):
        y_pred, filter_class = single_filter_output(filter_params, valid_samples, mp)
        filter_diff[i] = np.mean(y_pred[y_true == filter_class]) -\
                         np.mean(y_pred[y_true != filter_class])
    return filter_diff

def get_filters_regression(filters, scaler, valid_samples, valid_phenotypes, mp):
    y_true = np.array(valid_phenotypes)
    filter_tau = np.zeros(len(filters))

    if scaler is not None:
        valid_samples = copy.deepcopy(valid_samples)
        valid_samples = [scaler.transform(x) for x in valid_samples]

    for i, filter_params in enumerate(filters):
        y_pred, _dummy = single_filter_output(filter_params, valid_samples, mp)
        # compute Kendall's tau for filter i
        w_out = filter_params[-1]
        filter_tau[i] = stats.kendalltau(y_true, w_out * y_pred)[0]
    return filter_tau

def get_selected_cells(filter_w, data, scaler=None, filter_response_thres=0,
                       export_continuous=False):
    nmark = data.shape[1]
    if scaler is not None:
        data = scaler.transform(data)
    w, b = filter_w[:nmark], filter_w[nmark]
    g = np.sum(w.reshape(1, -1) * data, axis=1) + b
    if export_continuous:
        g = relu(g).reshape(-1, 1)
        g_thres = (g > filter_response_thres).reshape(-1, 1)
        return np.hstack([g, g_thres])
    else:
        return (g > filter_response_thres).astype(int)

def create_graph(x1, k, g1=None, add_filter_response=False):

    # compute pairwise distances between all points
    # optionally, add cell filter activity as an extra feature
    if add_filter_response:
        x1 = np.hstack([x1, g1.reshape(-1, 1)])

    d = pairwise_distances(x1, metric='euclidean')
    # create a k-NN graph
    idx = np.argsort(d)[:, 1:k+1]
    d.sort()
    d = d[:, 1:k+1]

    # create a weighted adjacency matrix from the distances (use gaussian kernel)
    # code from https://github.com/mdeff/cnn_graph/blob/master/lib/graph.py
    gauss_sigma = np.mean(d[:, -1])**2
    w = np.exp(- d**2 / gauss_sigma)

    # weight matrix
    M = x1.shape[0]
    I = np.arange(0, M).repeat(k)
    J = idx.reshape(M*k)
    V = w.reshape(M*k)
    W = coo_matrix((V, (I, J)), shape=(M, M))
    W.setdiag(0)
    adj = W.todense()

    # now reweight graph edges according to cell filter response similarity
    #def min_kernel(v):
    #   xv, yv = np.meshgrid(v, v)
    #   return np.minimum(xv, yv)
    #activity_kernel = pairwise_kernels(g1.reshape(-1, 1), g1.reshape(-1, 1), metric="rbf")
    #activity_kernel = min_kernel(g1)
    #adj = np.multiply(activity_kernel, adj)

    # create a graph from the adjacency matrix
    # first add the adges (binary matrix)
    G = igraph.Graph.Adjacency((adj > 0).tolist())
    # specify that the graph is undirected
    G.to_undirected()
    # now add weights to the edges
    G.es['weight'] = adj[adj.nonzero()]
    # print a summary of the graph
    igraph.summary(G)
    return G
