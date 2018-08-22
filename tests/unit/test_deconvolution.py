import pytest
from os import path
import numpy as np
import yass
from yass import preprocess, detect, cluster, templates, deconvolute
from util import ReferenceTesting


def test_decovnolution(path_to_threshold_config,
                       make_tmp_folder):
    yass.set_config('tests/config_nnet.yaml')

    # FIXME: hacky solution for the test to pass, i need to re-train the
    # triage network
    CONFIG = yass.read_config()
    d = CONFIG.detect._data
    d['neural_network_triage']['threshold_collision'] = 0
    CONFIG._set_param('detect', d)

    (standarized_path,
     standarized_params,
     whiten_filter) = preprocess.run(output_directory=make_tmp_folder)

    (score,
     spike_index_clear,
     spike_index_all) = detect.run(standarized_path,
                                   standarized_params,
                                   whiten_filter,
                                   output_directory=make_tmp_folder)

    spike_train_clear, tmp_loc, vbParam = cluster.run(
        score, spike_index_clear,
        output_directory=make_tmp_folder)

    (templates_, spike_train,
     groups, idx_good_templates) = templates.run(
        spike_train_clear, tmp_loc,
        output_directory=make_tmp_folder)

    deconvolute.run(spike_index_all, templates_,
                    output_directory=make_tmp_folder)


@pytest.mark.xfail
def test_deconvolution_returns_expected_results(path_to_threshold_config,
                                                path_to_output_reference,
                                                make_tmp_folder):
    np.random.seed(0)

    yass.set_config(path_to_threshold_config)

    (standarized_path,
        standarized_params,
        whiten_filter) = preprocess.run(output_directory=make_tmp_folder)

    (score, spike_index_clear,
     spike_index_all) = detect.run(standarized_path,
                                   standarized_params,
                                   whiten_filter,
                                   output_directory=make_tmp_folder)

    (spike_train_clear,
        tmp_loc,
        vbParam) = cluster.run(score, spike_index_clear,
                               output_directory=make_tmp_folder)

    (templates_, spike_train,
     groups,
     idx_good_templates) = templates.run(spike_train_clear, tmp_loc,
                                         output_directory=make_tmp_folder)

    spike_train = deconvolute.run(spike_index_all, templates_,
                                  output_directory=make_tmp_folder)

    path_to_spike_train = path.join(path_to_output_reference,
                                    'spike_train.npy')

    ReferenceTesting.assert_array_equal(spike_train, path_to_spike_train)