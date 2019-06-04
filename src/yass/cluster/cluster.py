# Class to do parallelized clustering
import os
import numpy as np
import networkx as nx
from sklearn.decomposition import PCA
from scipy.spatial import cKDTree
from scipy.stats import chi2
from scipy import interpolate

from yass.template import shift_chans, align_get_shifts_with_ref
from yass import mfm
from yass.util import absolute_path_to_asset
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
def warn(*args, **kwargs):
    pass
warnings.warn = warn

class Cluster(object):
    """Class for doing clustering."""

    def __init__(self, data_in, analysis=False):
            
        """Sets up the cluster class for each core
        Parameters: ...
              
        """
        # load data and check if prev completed
        if self.load_data(data_in):  return
        if analysis: return

        # local channel clustering
        if self.verbose:
            print("START LOCAL")
        # neighbour channel clustering
        self.initialize(indices_in=np.arange(len(self.spike_times_original)),
                        local=True)

        self.cluster(current_indices=np.arange(len(self.indices_in)),
                     local=True,
                     gen=0,
                     branch=0,
                     hist=[])
        #self.finish_plotting()
        
        if self.full_run:
            if self.verbose:
                print('START DISTANT')
            # distant channel clustering
            indices_train_local = np.copy(self.indices_train)
            indices_train_final = []
            templates_final = []
            for ii, indices_train_k in enumerate(indices_train_local):
                #if self.verbose: print("\nchan/unit {}, UNIT {}/{}".format(self.channel, ii, len(spike_train_local)))
                self.distant_ii = ii
                self.initialize(indices_in=indices_train_k,
                                local=False)
                self.cluster(current_indices=np.arange(len(self.indices_in)), local=False,
                             gen=self.history_local_final[ii][0]+1, 
                             branch=self.history_local_final[ii][1], 
                             hist=self.history_local_final[ii][1:])
                #self.finish_plotting(local_unit_id=ii)

                indices_train_final += self.indices_train
                templates_final += self.templates

        else:
            indices_train_final = []
            templates_final = []
            for indices_train_k in self.indices_train:
                template = self.get_templates_on_all_channels(indices_train_k)
                if self.check_max_chan(template):
                    templates_final.append(template)
                    indices_train_final.append(indices_train_k)
        
        if (self.full_run) and (not self.raw_data):
            templates_final_2 = []
            indices_train_final_2 = []
            for indices_train_k in indices_train_final:
                template = self.get_templates_on_all_channels(indices_train_k)
                if self.check_max_chan(template):
                    templates_final_2.append(template)
                    indices_train_final_2.append(indices_train_k)

            templates_final = templates_final_2
            indices_train_final = indices_train_final_2

        # save clusters
        self.save_result(indices_train_final, templates_final)

    def cluster(self, current_indices, local, gen, branch, hist):

        ''' Recursive clustering function
            channel: current channel being clusterd
            wf = wf_PCA: denoised waveforms (# spikes, # time points, # chans)
            sic = spike_indices of spikes on current channel
            gen = generation of cluster; increases with each clustering step        
            hist = is the current branch parent history
        '''

        if self.min(current_indices.shape[0]): return 

        if self.verbose:
            print("chan "+str(self.channel)+', gen '+str(gen)+', branch: ' +
                str(branch)+', # spikes: '+ str(current_indices.shape[0]))

        # featurize #1
        pca_wf = self.featurize_step(gen, current_indices, current_indices, local)

        # knn triage
        if self.raw_data:
            idx_keep = self.knn_triage_step(gen, pca_wf)
            pca_wf_triage = pca_wf[idx_keep]
        else:
            pca_wf_triage = np.copy(pca_wf)

        # subsample if too many
        pca_wf_subsample = self.subsample_step(gen, pca_wf_triage)
        # run mfm
        vbParam1 = self.run_mfm(gen, pca_wf_subsample)

        # recover spikes using soft-assignments
        idx_recovered, vbParam2 = self.recover_step(gen, vbParam1, pca_wf)
        if self.min(idx_recovered.shape[0]): return

        # if recovered spikes < total spikes, do further indexing
        if idx_recovered.shape[0] < pca_wf.shape[0]:
            current_indices = current_indices[idx_recovered]
            pca_wf = pca_wf[idx_recovered]

        # connecting clusters
        if vbParam2.rhat.shape[1] > 1:
            cc_assignment, stability, idx_keep = self.get_cc_and_stability(vbParam2)
            current_indices = current_indices[idx_keep]
            pca_wf = pca_wf[idx_keep]
        else:
            cc_assignment = np.zeros(pca_wf.shape[0], 'int32')
            stability = [1]

        # save generic metadata containing current branch info
        self.save_metadata(cc_assignment, pca_wf, current_indices, local,
                           gen, branch, hist)

        # single cluster
        if len(stability) == 1:
            self.single_cluster_step(current_indices, pca_wf, local,
                                     gen, branch, hist)

        # multiple clusters
        else:
            self.multi_cluster_step(current_indices, pca_wf, local,
                                    cc_assignment, gen, branch, hist)

    def save_metadata(self, label, pca_wf_all, current_indices, local,
                        gen, branch, hist):
        
        self.pca_post_triage_post_recovery.append(pca_wf_all)
        self.gen_label.append(label)
        #self.vbPar_muhat.append(vbParam2.muhat)

        # save history for every clustered distributions
        size_ = 2
        size_ += len(hist)
        temp = np.zeros(size_, 'int32')
        temp[0]=gen
        temp[1:-1]=hist
        temp[-1]=branch
        self.hist.append(temp)
        
        # save history again if local clustering converges in order to do
        # distant clustering tracking
        self.hist_local = temp

        if gen==0 and local:
            #self.pca_wf_allchans = self.pca_wf_allchans#[current_indices]
            self.indices_gen0 = current_indices
        
    def min(self, n_spikes):
        ''' Function that checks if spikes left are lower than min_spikes
        '''
        if n_spikes < self.min_spikes: 
            return True
        
        return False

    def load_data(self, data_in):

        ''' *******************************************
            ************ LOADED PARAMETERS ************
            *******************************************
        '''

        # load all input
        self.raw_data = data_in[0]
        self.full_run = data_in[1]
        self.CONFIG = data_in[2]
        self.reader_raw = data_in[3]
        self.reader_resid = data_in[4]
        self.filename_postclustering = data_in[5]
        
        if os.path.exists(self.filename_postclustering):
            return True
        else:
            input_data = np.load(data_in[6])
            self.spike_times_original = input_data['spike_times']
            if not self.raw_data:
                self.upsampled_templates = input_data['up_templates']
                self.upsampled_ids = input_data['up_ids']

        # if there is no spike to cluster, finish
        if len(self.spike_times_original) == 0:
            return True

        ''' ******************************************
            *********** FIXED PARAMETERS *************
            ****************************************** 
        '''

        # These are not user/run specific, should be stayed fixed
        self.verbose = False
        self.selected_PCA_rank = 5
        # threshold at which to set soft assignments to 0
        self.assignment_delete_threshold = 0.001
        # spike size
        self.spike_size = self.CONFIG.spike_size
        self.neighbors = self.CONFIG.neigh_channels
        # max number of spikes for each mfm call
        self.max_mfm_spikes = 10000
        # TODO: should be merged with min_spikes below
        #if self.raw_data:
        #    min_fr = 3
        #else:
        #    min_fr = 0.1
        #min_fr_triage = 3
        # min spikes : max time (length of recording) x min fr
        #self.n_sec_data = np.max(
        #    self.spike_times_original)/float(
        #    self.CONFIG.recordings.sampling_rate)
        #self.min_spikes_triage = int(self.n_sec_data*min_fr_triage)
        # if it will be subsampled, min spikes should decrease also
        #self.min_spikes_triage = int(self.min_spikes_triage*np.min((
        #    1, self.CONFIG.cluster.max_n_spikes/
        #    float(len(self.spike_times_original)))))
        # TODO: eventually, need to be merged with min_fr
        # should be a function of firing rate
        # should be in CONFIG file
        min_fr = 0.1
        n_sec_data = np.max(
            self.spike_times_original)/float(
            self.CONFIG.recordings.sampling_rate)
        self.min_spikes = int(n_sec_data*min_fr)
        # if it will be subsampled, min spikes should decrease also
        self.min_spikes = int(self.min_spikes*np.min((
            1, self.CONFIG.cluster.max_n_spikes/
            float(len(self.spike_times_original)))))
        # min_spikes needs to be at least 1
        self.min_spikes = max(self.min_spikes, 1)

        # load template space
        self.initialize_template_space()

        # random subsample, remove edge spikes
        # Cat: TODO: if edge spikes removed, can simplify binary reader to not track skipped spikes
        self.clean_input_spikes()

        # if there is no spike to cluster, finish
        if len(self.spike_times_original) == 0:
            return True

        ''' ******************************************
            *********** SAVING PARAMETERS ************
            ****************************************** 
        '''

        # flag to load all chans waveforms and featurizat for ari's work
        self.ari_flag = False
        self.wf_global_allchans = None
        self.pca_wf_allchans = None
        self.indices_gen0 = None
        self.data_to_fit = None
        self.pca_wf_gen0 = None

        # list that holds all the final clustered indices for the premerge clusters
        self.clustered_indices_local = []
        self.clustered_indices_distant = []
        
        # keep track of local idx source for distant clustering in order to 
        # index into original distribution indexes        
        self.distant_ii = None

        # initialize metadata saves; easier to do here than using local flags + conditional
        self.pca_post_triage_post_recovery=[]
        #self.vbPar_rhat=[]
        #self.vbPar_muhat=[]
        self.hist=[]
        self.gen_label = []

        # this list track the first clustering indexes
        self.history_local_final=[]
            
        # return flag that clustering not yet complete
        return False

    def clean_input_spikes(self):
        # limit clustering to at most 50,000 spikes
        max_spikes = self.CONFIG.cluster.max_n_spikes
        if len(self.spike_times_original)>max_spikes:
            idx_sampled = np.random.choice(
                a=np.arange(len(self.spike_times_original)),
                size=max_spikes,
                replace=False)
            self.spike_times_original = self.spike_times_original[idx_sampled]
        else:
            idx_sampled = np.arange(len(self.spike_times_original))

        # limit indexes away from edge of recording
        idx_inbounds = np.where(np.logical_and(
                        self.spike_times_original>=self.spike_size//2,
                        self.spike_times_original<(self.reader_raw.rec_len-self.spike_size)))[0]
        self.spike_times_original = self.spike_times_original[
            idx_inbounds].astype('int32')
        
        # clean upsampled ids if available
        if not self.raw_data:
            self.upsampled_ids = self.upsampled_ids[
                idx_sampled][idx_inbounds].astype('int32')

    def initialize_template_space(self):

        # load template space related files
        self.pca_main_components_= np.load(absolute_path_to_asset(
            os.path.join('template_space', 'pca_main_components.npy')))
        self.pca_sec_components_ = np.load(absolute_path_to_asset(
            os.path.join('template_space', 'pca_sec_components.npy')))

        self.pca_main_noise_std = np.load(absolute_path_to_asset(
            os.path.join('template_space', 'pca_main_noise_std.npy')))
        self.pca_sec_noise_std = np.load(absolute_path_to_asset(
            os.path.join('template_space', 'pca_sec_noise_std.npy')))

        # ref template
        self.ref_template = np.load(absolute_path_to_asset(
            os.path.join('template_space', 'ref_template.npy')))

        # upsample templates so that they match raw data sampling rate
        #self.upsample_template_space()

        # turn off edges for less collision
        window = np.int32(np.int32([15, 40])*self.spike_size/61.)
        self.pca_main_components_[:, :window[0]] = 0
        self.pca_main_components_[:, window[1]:] = 0
        self.pca_sec_components_[:, :window[0]] = 0
        self.pca_sec_components_[:, window[1]:] = 0

    def upsample_template_space(self):
        ''' Function that upsamples/downsamples the PCA filters
            in order to match them to the sampling rate of the data.
            Note: PCA space projection/denoising may eventually be repaced
        '''
        
        # set params for upsampling
        spike_size=self.spike_size
        x = np.arange(self.pca_main_components_.shape[1])
        xnew = np.linspace(0, x.shape[0]-1, num=spike_size, endpoint=True)

        pca_main_components_new = []
        pca_sec_components_new = []
        for k in range(5):
            # interpolate pca_main comps
            y = self.pca_main_components_[k]
            tck = interpolate.splrep(x, y, s=0)
            ynew = interpolate.splev(xnew, tck, der=0)
            pca_main_components_new.append(ynew)

            # interpolate second comps std
            y = self.pca_sec_components_[k]
            tck = interpolate.splrep(x, y, s=0)
            ynew = interpolate.splev(xnew, tck, der=0)
            pca_sec_components_new.append(ynew)

        self.pca_main_components_ = np.vstack(pca_main_components_new)
        self.pca_sec_components_ = np.vstack(pca_sec_components_new)

        # upsample reference template
        y = self.ref_template
        tck = interpolate.splrep(x, y, s=0)
        self.ref_template = interpolate.splev(xnew, tck, der=0)

    def initialize(self, indices_in, local):

        # reset spike_train and templates for both local and distant clustering
        self.indices_train = []
        self.templates = []
        if local:
            # initialize
            self.shifts = np.zeros(len(self.spike_times_original))
            self.find_main_channel()

        self.indices_in = indices_in

        # load waveforms
        if len(self.indices_in) > 0:
            self.load_waveforms(local)
            # align waveforms
            self.align_step(local)
            # denoise waveforms on active channels
            self.denoise_step(local)
       
    def find_main_channel(self):
        if len(self.spike_times_original) > 500:
            idx_sampled = np.random.choice(
                a=np.arange(len(self.spike_times_original)),
                size=500,
                replace=False)
        else:
            idx_sampled = np.arange(len(self.spike_times_original))

        sample_spike_times = self.spike_times_original[idx_sampled]

        if self.raw_data:
            wf, _ = self.reader_raw.read_waveforms(
                sample_spike_times, self.spike_size)
        # or from residual and add templates
        else:
            units_ids_sampled = self.upsampled_ids[idx_sampled]
            wf, _ = self.reader_resid.read_clean_waveforms(
                sample_spike_times, units_ids_sampled,
                self.upsampled_templates, self.spike_size)

        # find max channel
        self.channel = np.mean(wf, axis=0).ptp(0).argmax()

    def load_waveforms(self, local):
        
        '''  Waveforms only loaded once in gen0 before local clustering starts
        '''

        if self.verbose:
            print ("chan "+str(self.channel)+", loading {} waveforms".format(
                len(self.indices_in)))

        self.neighbor_chans = np.where(self.neighbors[self.channel])[0]

        if local:
            self.loaded_channels = self.neighbor_chans
        else:
            self.loaded_channels = np.arange(self.reader_raw.n_channels)

        # load waveforms from raw data 
        spike_times = self.spike_times_original[self.indices_in]
        if self.raw_data:
            self.wf_global, skipped_idx = self.reader_raw.read_waveforms(
                spike_times, self.spike_size, self.loaded_channels)
        # or from residual and add templates
        else:
            unit_ids = self.upsampled_ids[self.indices_in]
            self.wf_global, skipped_idx = self.reader_resid.read_clean_waveforms(
                spike_times, unit_ids, self.upsampled_templates,
                self.spike_size, self.loaded_channels)
        
        # clip waveforms; seems necessary for neuropixel probe due to artifacts
        self.wf_global = self.wf_global.clip(min=-1000, max=1000)

        # delete any spikes that could not be loaded in previous step
        if len(skipped_idx)>0:
            self.indices_in = np.delete(self.indices_in, skipped_idx)


    def align_step(self, local):

        if self.verbose:
            print ("chan "+str(self.channel)+", aligning")
        
        # align waveforms by finding best shfits
        if local:
            mc = np.where(self.loaded_channels==self.channel)[0][0]
            best_shifts = align_get_shifts_with_ref(
                self.wf_global[:, :, mc],
                self.ref_template)
            self.shifts[self.indices_in] = best_shifts
        else:
            best_shifts = self.shifts[self.indices_in]
        
        self.wf_global = shift_chans(self.wf_global, best_shifts)

        if self.ari_flag:
            pass
            #self.wf_global_allchans = shift_chans(self.wf_global_allchans, 
            #                                         best_shifts)

    def denoise_step(self, local):

        if local:
            self.denoise_step_local()
        else:
            self.denoise_step_distant2()

        if self.verbose:
            print ("chan "+str(self.channel)+", waveorms denoised to {} dimensions".format(self.denoised_wf.shape[1]))

    def denoise_step_local(self):

        # align, note: aligning all channels to max chan which is appended to the end
        # note: max chan is first from feat_chans above, ensure order is preserved
        # note: don't want for wf array to be used beyond this function
        # Alignment: upsample max chan only; linear shift other chans
        n_data, _, n_chans = self.wf_global.shape
        self.denoised_wf = np.zeros((n_data, self.pca_main_components_.shape[0], n_chans),
                                    dtype='float32')

        for ii in range(n_chans):
            if self.loaded_channels[ii] == self.channel:
                self.denoised_wf[:, :, ii] = np.matmul(
                    self.wf_global[:, :, ii],
                    self.pca_main_components_.T)/self.pca_main_noise_std[np.newaxis]
            else:
                self.denoised_wf[:, :, ii] = np.matmul(
                    self.wf_global[:, :, ii],
                    self.pca_sec_components_.T)/self.pca_sec_noise_std[np.newaxis]

        self.denoised_wf = np.reshape(self.denoised_wf, [n_data, -1])

        #energy = np.median(np.square(self.denoised_wf), axis=0)
        #good_features = np.where(energy > 0.5)[0]
        #if len(good_features) < self.selected_PCA_rank:
        #    good_features = np.argsort(energy)[-self.selected_PCA_rank:]
        #self.denoised_wf = self.denoised_wf[:, good_features]

    def denoise_step_distant(self):

        # active locations with negative energy
        energy = np.median(np.square(self.wf_global), axis=0)
        good_t, good_c = np.where(energy > 0.5)

        # limit to max_timepoints per channel
        max_timepoints = 3
        unique_channels = np.unique(good_c)
        idx_keep = np.zeros(len(good_t), 'bool')
        for channel in unique_channels:
            idx_temp = np.where(good_c == channel)[0]
            if len(idx_temp) > max_timepoints:
                idx_temp = idx_temp[
                    np.argsort(
                        energy[good_t[idx_temp], good_c[idx_temp]]
                    )[-max_timepoints:]]
            idx_keep[idx_temp] = True

        good_t = good_t[idx_keep]
        good_c = good_c[idx_keep]

        if len(good_t) == 0:
            good_t, good_c = np.where(energy == np.max(energy))

        self.denoised_wf = self.wf_global[:, good_t, good_c]

    def denoise_step_distant2(self):

        # active locations with negative energy
        #energy = np.median(np.square(self.wf_global), axis=0)
        #template = np.median(self.wf_global, axis=0)
        #good_t, good_c = np.where(np.logical_and(energy > 0.5, template < - 0.5))
        template = np.median(self.wf_global, axis=0)
        good_t, good_c = np.where(template < - 0.5)
        th = np.max((-0.5, np.min(template[:, self.channel])))

        t_diff = 1
        # lowest among all
        #main_c_loc = np.where(good_c==self.channel)[0]
        #max_chan_energy = energy[good_t[main_c_loc]][:,self.channel]
        #index = main_c_loc[np.argmax(max_chan_energy)]
        index = template[good_t, good_c].argmin()
        keep = connecting_points(np.vstack((good_t, good_c)).T, index, self.neighbors, t_diff)
        good_t = good_t[keep]
        good_c = good_c[keep]

        # limit to max_timepoints per channel
        max_timepoints = 3
        unique_channels = np.unique(good_c)
        idx_keep = np.zeros(len(good_t), 'bool')
        for channel in unique_channels:
            idx_temp = np.where(good_c == channel)[0]
            if len(idx_temp) > max_timepoints:
                idx_temp = idx_temp[np.argsort(
                    template[good_t[idx_temp], good_c[idx_temp]])[:max_timepoints]]
            idx_keep[idx_temp] = True
        good_t = good_t[idx_keep]
        good_c = good_c[idx_keep]

        self.denoised_wf = self.wf_global[:, good_t, good_c]

    def denoise_step_distant3(self):

        energy = np.median(self.wf_global, axis=0)
        max_energy = np.min(energy, axis=0)

        # max_energy_loc is n x 2 matrix, where each row has time point and channel info
        th = np.max((-0.5, max_energy[self.channel]))
        max_energy_loc_c = np.where(max_energy <= th)[0]
        max_energy_loc_t = energy.argmin(axis=0)
        max_energy_loc = np.hstack((max_energy_loc_t[max_energy_loc_c][:, np.newaxis],
                                    max_energy_loc_c[:, np.newaxis]))

        t_diff = 3
        main_channel_loc = np.where(self.loaded_channels == self.channel)[0][0]
        index = np.where(max_energy_loc[:,1]== main_channel_loc)[0][0]
        keep = connecting_points(max_energy_loc, index, self.neighbors, t_diff)

        max_energy_loc = max_energy_loc[keep]

        # exclude main and secondary channels
        #if np.sum(~np.in1d(max_energy_loc[:,1], self.neighbor_chans)) > 0:
        #    max_energy_loc = max_energy_loc[~np.in1d(max_energy_loc[:,1], self.neighbor_chans)]
        #else:
        #    max_energy_loc = max_energy_loc[max_energy_loc[:,1]==main_channel_loc]

        # denoised wf in distant channel clustering is 
        # the most active time point in each active channels
        self.denoised_wf = np.zeros((self.wf_global.shape[0], len(max_energy_loc)), dtype='float32')
        for ii in range(len(max_energy_loc)):
            self.denoised_wf[:, ii] = self.wf_global[:, max_energy_loc[ii,0], max_energy_loc[ii,1]]

    def featurize_step(self, gen, indices_to_feat, indices_to_transform, local):
        ''' Indices hold the index of the current spike times relative all spikes
        '''
        
        if self.verbose:
            print("chan "+str(self.channel)+', gen '+str(gen)+', featurizing')

        # find high variance area. 
        # Including low variance dimensions can lead to overfitting 
        # (splitting based on collisions)
        rank = min(len(indices_to_feat), self.denoised_wf.shape[1], self.selected_PCA_rank)
        #stds = np.std(self.denoised_wf[indices_to_feat], axis=0)
        #good_d = np.where(stds > 1.05)[0]
        #if len(good_d) < rank:
        #    good_d = np.argsort(stds)[::-1][:rank]

        pca = PCA(n_components=rank)
        #pca.fit(self.denoised_wf[indices_to_feat][:, good_d])
        #pca_wf = pca.transform(
        #    self.denoised_wf[indices_to_transform][:, good_d]).astype('float32')

        pca.fit(self.denoised_wf[indices_to_feat])
        pca_wf = pca.transform(
            self.denoised_wf[indices_to_transform]).astype('float32')

        if gen==0 and local:
            # save gen0 distributions before triaging
            #data_to_fit = self.denoised_wf[:, good_d]
            #n_samples, n_features = data_to_fit.shape
            #pca = PCA(n_components=min(self.selected_PCA_rank, n_features))
            #pca_wf_gen0 = pca.fit_transform(data_to_fit)
            #self.pca_wf_gen0 = pca_wf_gen0.copy()
            self.pca_wf_gen0 = pca_wf.copy()

        if self.ari_flag and gen==0 and local:
            # Cat: TODO: do this only once per channel
            #  Also, do not index into wf_global_allchans; that's done at completion
            #if self.wf_global_allchans.shape[1] > self.selected_PCA_rank:
            
            # denoise global data:

            wf_global_denoised = self.denoise_step_distant_all_chans()
            
            # flatten data over last 2 dimensions first
            n_data, _ = wf_global_denoised.shape
            wf_allchans_2D = wf_global_denoised
            
            stds = np.std(wf_allchans_2D, axis=0)
            good_d = np.where(stds > 1.05)[0]
            if len(good_d) < self.selected_PCA_rank:
                good_d = np.argsort(stds)[::-1][:self.selected_PCA_rank]

            data_to_fit = wf_allchans_2D[:, good_d]
            n_samples, n_features = data_to_fit.shape
            pca = PCA(n_components=min(self.selected_PCA_rank, n_features))
            
            # keep original uncompressed data
            self.data_to_fit = data_to_fit

            # compress data to selectd pca rank
            self.pca_wf_allchans = pca.fit_transform(data_to_fit)
            
        return pca_wf        

    def subsample_step(self, gen, pca_wf):
 
        if self.verbose:
            print("chan "+str(self.channel)+', gen '+str(gen)+', random subsample')

        if pca_wf.shape[0]> self.max_mfm_spikes:
            #if self.full_run:
            if True:
                idx_subsampled = coreset(
                    pca_wf, self.max_mfm_spikes)
            else:
                idx_subsampled = np.random.choice(np.arange(pca_wf.shape[0]),
                                 size=self.max_mfm_spikes,
                                 replace=False)
                
            pca_wf = pca_wf[idx_subsampled]

        return pca_wf

    def run_mfm(self, gen, pca_wf):

        mask = np.ones((pca_wf.shape[0], 1))
        group = np.arange(pca_wf.shape[0])
        vbParam = mfm.spikesort(pca_wf[:,:,np.newaxis],
                                mask,
                                group,
                                self.CONFIG)

        if self.verbose:
            print("chan "+ str(self.channel)+', gen '\
                +str(gen)+", "+str(vbParam.rhat.shape[1])+" clusters from ",pca_wf.shape)

        return vbParam

    def knn_triage_step(self, gen, pca_wf):

        if self.verbose:
            print("chan "+str(self.channel)+', gen '+str(gen)+', knn triage')

        self.triage_value = 0.01
        knn_triage_threshold = 100*(1-self.triage_value)

        if pca_wf.shape[0] > 1/self.triage_value:
            idx_keep = knn_triage(knn_triage_threshold, pca_wf)
            idx_keep = np.where(idx_keep==1)[0]
        else:
            idx_keep = np.arange(pca_wf.shape[0])

        return idx_keep

    def knn_triage_dynamic(self, gen, vbParam, pca_wf):

        ids = np.where(vbParam.nuhat > self.min_spikes)[0]

        if ids.size <= 1:
            self.triage_value = 0
            return np.arange(pca_wf.shape[0])

        muhat = vbParam.muhat[:,ids,0].T
        cov = vbParam.invVhat[:,:,ids,0].T / vbParam.nuhat[ids,np.newaxis, np.newaxis]

        # Cat: TODO: move to CONFIG/init function
        min_spikes = min(self.min_spikes_triage, pca_wf.shape[0]//ids.size) ##needs more systematic testing, working on it

        pca_wf_temp = np.zeros([min_spikes*cov.shape[0], cov.shape[1]])
        #assignment_temp = np.zeros(min_spikes*cov.shape[0], dtype = int)
        for i in range(cov.shape[0]):
            pca_wf_temp[i*min_spikes:(i+1)*min_spikes]= np.random.multivariate_normal(muhat[i], cov[i], min_spikes)
            #assignment_temp[i*min_spikes:(i+1)*min_spikes] = i

        kdist_temp = knn_dist(pca_wf_temp)
        kdist_temp = kdist_temp[:,1:]

        median_distances = np.zeros([cov.shape[0]])
        for i in range(median_distances.shape[0]):
            #median_distances[i] = np.median(np.median(kdist_temp[i*min_spikes:(i+1)*min_spikes], axis = 0), axis = 0)
            median_distances[i] = np.percentile(np.sum(kdist_temp[i*min_spikes:(i+1)*min_spikes], axis = 1), 90)

        ## The percentile value also needs to be tested, value of 50 and scale of 1.2 works wells
        kdist = np.sum(knn_dist(pca_wf)[:, 1:], axis=1)
        min_threshold = np.percentile(kdist, 100*float(self.CONFIG.cluster.min_spikes)/len(kdist))
        threshold = max(np.median(median_distances), min_threshold)
        idx_keep = kdist <= threshold
        self.triage_value = 1.0 - idx_keep.sum()/idx_keep.size

        if np.sum(idx_keep) < self.min_spikes:
            raise ValueError("{} kept out of {}, min thresh: {}, actual threshold {}, max dist {}".format(idx_keep.sum(),idx_keep.size, min_threshold, threshold, np.max(kdist)))

        if self.verbose:
            print("chan "+str(self.channel)+', gen '+str(gen)+', '+str(np.round(self.triage_value*100))+'% triaged from adaptive knn triage')

        return np.where(idx_keep)[0]

    def recover_step(self, gen, vbParam, pca_wf_all):
 
        # for post-deconv reclustering, we can safely cluster only 10k spikes or less
        idx_recovered, vbParam = self.recover_spikes(vbParam, pca_wf_all)

        if self.verbose:
            print ("chan "+ str(self.channel)+', gen '+str(gen)+", recovered ",
                                                str(idx_recovered.shape[0])+ " spikes")

        return idx_recovered, vbParam
    
    def recover_spikes(self, vbParam, pca, maha_dist=1):
    
        N, D = pca.shape
        # Cat: TODO: check if this maha thresholding recovering distance is good
        threshold = np.sqrt(chi2.ppf(0.99, D))

        # update rhat on full data
        maskedData = mfm.maskData(pca[:,:,np.newaxis], np.ones([N, 1]), np.arange(N))
        vbParam.update_local(maskedData)

        # calculate mahalanobis distance
        maha = mfm.calc_mahalonobis(vbParam, pca[:,:,np.newaxis])
        idx_recovered = np.where(~np.all(maha >= threshold, axis=1))[0]
        vbParam.rhat = vbParam.rhat[idx_recovered]

        # zero out low assignment vals
        if True:
            vbParam.rhat[vbParam.rhat < self.assignment_delete_threshold] = 0
            vbParam.rhat = vbParam.rhat/np.sum(vbParam.rhat,
                                             1, keepdims=True)

        return idx_recovered, vbParam

    def calculate_stability(self, rhat):
        K = rhat.shape[1]
        mask = rhat > 0.0
        stability = np.zeros(K)
        for clust in range(stability.size):
            if mask[:,clust].sum() == 0.0:
                continue
            stability[clust] = np.average(mask[:,clust] * rhat[:,clust], axis = 0, weights = mask[:,clust])

        return stability

    def get_k_cc(self, maha, maha_thresh_min, k_target):

        # it assumes that maha_thresh_min gives 
        # at least k+1 number of connected components
        k_now = k_target + 1
        if len(self.get_cc(maha, maha_thresh_min)) != k_now:
            raise ValueError("something is not right")

        maha_thresh = maha_thresh_min
        while k_now > k_target:
            maha_thresh += 1
            cc = self.get_cc(maha, maha_thresh)
            k_now = len(cc)

        if k_now == k_target:
            return cc, maha_thresh

        else:
            maha_thresh_max = maha_thresh
            maha_thresh_min = maha_thresh - 1
            if len(self.get_cc(maha, maha_thresh_min)) <= k_target:
                raise ValueError("something is not right")

            ctr = 0
            maha_thresh_max_init = maha_thresh_max
            while True:
                ctr += 1
                maha_thresh = (maha_thresh_max + maha_thresh_min)/2.0
                cc = self.get_cc(maha, maha_thresh)
                k_now = len(cc)
                if k_now == k_target:
                    return cc, maha_thresh
                elif k_now > k_target:
                    maha_thresh_min = maha_thresh
                elif k_now < k_target:
                    maha_thresh_max = maha_thresh

                if ctr > 1000:
                    print(k_now, k_target, maha_thresh, maha_thresh_max_init)
                    print(cc)
                    print(len(self.get_cc(maha, maha_thresh+0.001)))
                    print(len(self.get_cc(maha, maha_thresh-0.001)))
                    raise ValueError("something is not right")


    def get_cc(self, maha, maha_thresh):
        row, column = np.where(maha<maha_thresh)
        G = nx.DiGraph()
        for i in range(maha.shape[0]):
            G.add_node(i)
        for i, j in zip(row,column):
            G.add_edge(i, j)
        cc = [list(units) for units in nx.strongly_connected_components(G)]
        return cc

    def cluster_annealing(self, vbParam):

        N, K = vbParam.rhat.shape

        stability = self.calculate_stability(vbParam.rhat)
        if (K == 2) or np.all(stability > 0.9):
            cc = [[k] for k in range(K)]
            return vbParam.rhat.argmax(1), stability, cc

        maha = mfm.calc_mahalonobis(vbParam, vbParam.muhat.transpose((1,0,2)))
        maha = np.maximum(maha, maha.T)
        #N, K = vbParam.rhat.shape
        #mu = np.copy(vbParam.muhat[:,:,0].T)
        #mudiff = mu[:,np.newaxis] - mu
        #prec = vbParam.Vhat[:,:,:,0].T * vbParam.nuhat[:,np.newaxis, np.newaxis]
        #maha = np.matmul(np.matmul(mudiff[:, :, np.newaxis], prec[:, np.newaxis]), mudiff[:, :, :, np.newaxis])[:, :, 0, 0]

        # decrease number of connected components one at a time.
        # in any step if all components are stables, stop and return
        # otherwise, go until there are only two connected components and return it
        maha_thresh_min = 0
        for k_target in range(K-1, 1, -1):
            # get connected components with k_target number of them
            cc, maha_thresh_min = self.get_k_cc(maha, maha_thresh_min, k_target)
            # calculate soft assignment for each cc
            rhat_cc = np.zeros([N,len(cc)])
            for i, units in enumerate(cc):
                rhat_cc[:, i] = np.sum(vbParam.rhat[:, units], axis=1)
            rhat_cc[rhat_cc<0.001] = 0.0
            rhat_cc = rhat_cc/np.sum(rhat_cc,axis =1 ,keepdims = True)

            # calculate stability for each component
            # and make decision            
            stability = self.calculate_stability(rhat_cc)
            if np.all(stability>0.90) or k_target == 2:
                return rhat_cc.argmax(1), stability, cc

    def get_cc_and_stability(self, vbParam):

        cc_assignment, stability, cc = self.cluster_annealing(vbParam)
        n_counts = np.zeros(len(cc), 'int32')
        unique_ccs, n_counts_unique = np.unique(cc_assignment, return_counts=True)
        n_counts[unique_ccs] = n_counts_unique
        idx_keep = np.arange(len(cc_assignment))

        while np.min(n_counts) < self.min_spikes and np.max(n_counts) >= self.min_spikes:
            cc_keep = np.where(n_counts >= self.min_spikes)[0]
            idx_keep_current = np.where(np.in1d(cc_assignment, cc_keep))[0]
            vbParam.rhat = vbParam.rhat[idx_keep_current]
            k_keep = np.hstack([cc[c] for c in cc_keep])
            if len(k_keep) > 1:
                vbParam.rhat = vbParam.rhat[:,k_keep]
                vbParam.rhat = vbParam.rhat/np.sum(vbParam.rhat, axis=1, keepdims=True)
                vbParam.muhat = vbParam.muhat[:, k_keep]
                vbParam.Vhat = vbParam.Vhat[:, :, k_keep]
                vbParam.nuhat = vbParam.nuhat[k_keep]

                cc_assignment, stability, cc = self.cluster_annealing(vbParam)
                n_counts = np.zeros(np.max(cc_assignment)+1, 'int32')
                unique_ccs, n_counts_unique = np.unique(cc_assignment, return_counts=True)
                n_counts[unique_ccs] = n_counts_unique

            else:
                cc_assignment = np.zeros(len(idx_keep_current), 'int32')
                stability = [1]
                n_counts = [len(idx_keep_current)]

            idx_keep = idx_keep[idx_keep_current]

        return cc_assignment, stability, idx_keep

    def single_cluster_step(self, current_indices, pca_wf, local,
                            gen, branch, hist):

        # exclude units whose maximum channel is not on the current 
        # clustered channel; but only during clustering, not during deconv
        template = np.median(self.wf_global[current_indices], axis=0)
        #template = stats.trim_mean(self.wf_global[current_indices],
        #                           0.1, axis=0)
        #template = np.mean(self.wf_global[current_indices], axis=0)
        assignment = np.zeros(len(current_indices))
        mc = self.loaded_channels[np.argmax(template.ptp(0))]
        if (mc in self.neighbor_chans) or (not self.raw_data):        
            N = len(self.indices_train)
            if self.verbose:
                print("chan "+str(self.channel)+', gen '+str(gen)+", >>> cluster "+
                    str(N)+" saved, size: "+str(len(assignment))+"<<<")
                print ("")
            
            self.indices_train.append(self.indices_in[current_indices])
            self.templates.append(template)
            
                           
            # save meta data only for initial local group
            if local:
                self.clustered_indices_local.append(current_indices)
                
                # save the history chain for a completed unit clusterd locally
                # this is by distant clustering step by appending to list
                self.history_local_final.append(self.hist_local)
        
            else:
                # if distant cluster step, use indexes from local step 
                self.clustered_indices_distant.append(
                    self.clustered_indices_local[self.distant_ii][current_indices])
        else:
            if self.verbose:
                print ("  chan "+str(self.channel)+", template has maxchan "+str(mc), 
                        " skipping ...")  
             
    def multi_cluster_step(self, current_indices, pca_wf, local, cc_assignment,
                                gen, branch_current, hist):
        
        # if self.plotting and gen<20:
            # self.plot_clustering_scatter(gen, pca_wf, cc_assignment,
                                         # stability, 'multi split')

        # Cat: TODO: unclear how much memory this saves
        pca_wf = pca_subsampled = vbParam2 = None

        for branch_next, clust in enumerate(np.unique(cc_assignment)):
            idx = np.where(cc_assignment==clust)[0]

            if self.verbose:
                print("chan "+str(self.channel)+', gen '+str(gen)+
                    ", reclustering cluster with "+ str(idx.shape[0]) +' spikes')

            # add current branch info for child process
            # Cat: TODO: this list append is not pythonic
            local_hist=list(hist)
            local_hist.append(branch_current)
            self.cluster(current_indices[idx],local, gen+1, branch_next,
                         local_hist)

    def get_templates_on_all_channels(self, indices_in):
        
        self.indices_in = indices_in

        local=False
        
        # temporarily change raw_data option to True
        self_raw_data_orig = np.copy(self.raw_data)
        self.raw_data = True

        self.load_waveforms(local)
        self.align_step(local)

        template = np.median(self.wf_global, axis=0)
        
        # change raw_data option back to the orignal
        self.raw_data = self_raw_data_orig

        return template
    
    def check_max_chan(self, template):
        
        mc = template.ptp(0).argmax()
        
        if np.any(self.neighbor_chans == mc):
            return True
        else:
            return False

    def save_result(self, indices_train, templates):

        # Cat: TODO: note clustering is done on PCA denoised waveforms but
        #            templates are computed on original raw signal
        # recompute templates to contain full width information... 
        # fixes numpy bugs

        spike_train = [self.spike_times_original[indices] - self.shifts[indices] for indices in indices_train]
        pca_post_triage_post_recovery = np.empty(
            len(self.pca_post_triage_post_recovery), dtype=object)
        pca_post_triage_post_recovery[:] = self.pca_post_triage_post_recovery

        np.savez(self.filename_postclustering,
                 spiketime=spike_train,
                 templates=templates,
                 gen0_fullrank = self.data_to_fit,
                 pca_wf_gen0=self.pca_wf_gen0,
                 pca_wf_gen0_allchans=self.pca_wf_allchans,
                 clustered_indices_local=self.clustered_indices_local,
                 clustered_indices_distant=self.clustered_indices_distant,
                 pca_post_triage_post_recovery = pca_post_triage_post_recovery,
                 spike_times_original = self.spike_times_original,
                 #vbPar_rhat = self.vbPar_rhat,
                 #vbPar_muhat = self.vbPar_muhat,
                 gen_label = self.gen_label,
                 hist = self.hist,
                 indices_gen0=self.indices_gen0,
                 #spike_index_prerecluster=self.original_indices,
                 #templates_prerecluster=self.template_original
                )

        if self.verbose:
            print(self.filename_postclustering)
            print("**** starting spikes: {}, found # clusters: {}".format(
                len(self.spike_times_original), len(spike_train)))
        # Cat: TODO: are these redundant?
        keys = []
        for key in self.__dict__:
            keys.append(key)
        for key in keys:
            delattr(self, key)

def knn_triage(th, pca_wf):

    tree = cKDTree(pca_wf)
    dist, ind = tree.query(pca_wf, k=6)
    dist = np.sum(dist, 1)

    idx_keep1 = dist <= np.percentile(dist, th)
    return idx_keep1

def knn_dist(pca_wf):
    tree = cKDTree(pca_wf)
    dist, ind = tree.query(pca_wf, k=30)
    return dist

def connecting_points(points, index, neighbors, t_diff, keep=None):

    if keep is None:
        keep = np.zeros(len(points), 'bool')

    if keep[index] == 1:
        return keep
    else:
        keep[index] = 1
        spatially_close = np.where(neighbors[points[index, 1]][points[:, 1]])[0]
        close_index = spatially_close[np.abs(points[spatially_close, 0] - points[index, 0]) <= t_diff]

        for j in close_index:
            keep = connecting_points(points, j, neighbors, t_diff, keep)

        return keep

def coreset(data, m, K=3, delta=0.01):
    p = int(np.ceil(np.log2(1/delta)))
    B = kmeans_init(data, K, p)
    a = 16*(np.log2(K) + 2)

    N = data.shape[0]
    dists = np.sum(np.square(data[:, None] - B[None]), axis=2)
    label = dists.argmin(1)
    dists = dists.min(1)

    dists_sum = np.sum(dists)
    dists_sum_k = np.zeros(K)
    for j in range(N):
        dists_sum_k[label[j]] += dists[j]
    _, n_data_k  = np.unique(label, return_counts=True)

    s = a*dists + 2*(a*dists_sum_k/n_data_k + dists_sum/n_data_k)[label]
    p = s/sum(s)

    idx_coreset = np.random.choice(N, size=m, replace=False, p=p)
    #weights = 1/(m*p[idx_coreset])
    #weights[weights<1] = 1
    #weights = np.ones(m)

    return idx_coreset#, weights

def kmeans_init(data, K, n_iter):
    N, D = data.shape
    centers = np.zeros((n_iter, K, D))
    dists = np.zeros(n_iter)
    for ctr in range(n_iter):
        ii = np.random.choice(N, size=1, replace=True, p=np.ones(N)/float(N))
        C = data[ii]
        L = np.zeros(N, 'int16')
        for i in range(1, K):
            D = data - C[L]
            D = np.sum(D*D, axis=1) #L2 dist
            ii = np.random.choice(N, size=1, replace=True, p=D/np.sum(D))
            C = np.concatenate((C, data[ii]), axis=0)
            L = np.argmax(
                2 * np.dot(C, data.T) - np.sum(C*C, axis=1)[:, np.newaxis],
                axis=0)

        centers[ctr] = C
        D = data - C[L]
        dists[ctr] = np.sum(np.square(D))

    return centers[np.argmin(dists)]
