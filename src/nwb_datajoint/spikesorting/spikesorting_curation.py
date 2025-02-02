import json
import os
import shutil
import uuid
from pathlib import Path
from warnings import WarningMessage

import datajoint as dj
import spikeinterface as si
import spikeinterface.toolkit as st

from ..common.common_interval import IntervalList
from ..common.common_nwbfile import AnalysisNwbfile
from ..common.dj_helper_fn import fetch_nwb
from .spikesorting_recording import SpikeSortingRecording
from .spikesorting_sorting import SpikeSorting

schema = dj.schema('spikesorting_curation')


@schema
class Curation(dj.Manual):
    definition = """
    # Stores each spike sorting; similar to IntervalList
    curation_id: varchar(10)
    -> SpikeSorting
    ---
    parent_curation_id='': varchar(10)
    labels: blob # a dictionary of labels for the units
    merge_groups: blob # a list of merge groups for the units
    metrics: blob # a list of quality metrics for the units (if available)
    description='': varchar(1000) #optional description for this curated sort
    """

    @staticmethod
    def insert_curation(sorting_key: dict, parent_curation_id: str = 'original',
                        labels=None, merge_groups=None, metrics=None,
                        description=''):
        """Given a SpikeSorting key and the `parent_sorting_id`
        (and optional arguments) insert an entry into Curation

        Parameters
        ----------
        sorting_key : dict
            the key for the original SpikeSorting
        parent_sorting_id : str
            the id of the parent sorting (optional)
        parent_sorting_id : str
        param metrics : dict
            computed metrics for sorting (optional)
        description : str
            text description of this sort (optional)
        labels : dict
            dictionary of labels for this sort (optional)

        Returns
        -------
        sorting_id : str

        """
        if labels is None:
            labels = {}
        if merge_groups is None:
            merge_groups = []
        if metrics is None:
            metrics = {}

        # generate a unique ID
        found = True
        while found:
            id = 'C_' + str(uuid.uuid4())[:8]
            if len((Curation & {'curation_id': id}).fetch()) == 0:
                found = False

        sorting_key['curation_id'] = id
        sorting_key['parent_curation_id'] = parent_curation_id
        sorting_key['description'] = description
        sorting_key['labels'] = labels
        sorting_key['merge_groups'] = merge_groups
        sorting_key['metrics'] = metrics

        Curation.insert1(sorting_key)

        return sorting_key['curation_id']

    @staticmethod
    def get_recording_extractor(key):
        """Returns the recording extractor for the recording related to this curation

        :param key: key to a single Curation entry
        :type key: dict

        returns
            recording_extractor: spike interface recording extractor
        """
        recording_path = (SpikeSortingRecording & key).fetch1('recording_path')
        return si.load_extractor(recording_path)

    @staticmethod
    def get_curated_sorting_extractor(key):
        """Returns the sorting extractor related to this curation, with merges applied

        :param key: key to a single Curation entry
        :type key: dict

        returns
            sorting_extractor: spike interface sorting extractor
        """
        sorting_path = (SpikeSorting & key).fetch1('sorting_path')
        sorting = si.load_extractor(sorting_path)
        merge_groups = (Curation & key).fetch1('merge_groups')
        # TODO: write code to get merged sorting extractor
        if len(merge_groups) != 0:

            return NotImplementedError
            # sorting = ...

        return sorting

    @staticmethod
    def save_sorting_nwb(key, sorting, timestamps, sort_interval_list_name,
                         sort_interval, labels=None, metrics=None,
                         unit_ids=None):
        """Store a sorting in a new AnalysisNwbfile
        Parameters
        ----------
        key : dict
            key to SpikeSorting table
        sorting : si.Sorting
            sorting
        timestamps : array_like
            Time stamps of the sorted recoridng;
            used to convert the spike timings from index to real time
        sort_interval_list_name : str
            name of sort interval
        sort_interval : list
            interval for start and end of sort
        labels : dict, optional
            curation labels, by default None
        metrics : dict, optional
            quality metrics, by default None
        unit_ids : list, optional
            IDs of units whose spiketrains to save, by default None

        Returns
        -------
        analysis_file_name : str
        units_object_id : str
        """

        sort_interval_valid_times = (
            IntervalList & {'interval_list_name': sort_interval_list_name}
        ).fetch1('valid_times')

        units = dict()
        units_valid_times = dict()
        units_sort_interval = dict()

        if unit_ids is None:
            unit_ids = sorting.get_unit_ids()

        for unit_id in unit_ids:
            spike_times_in_samples = sorting.get_unit_spike_train(
                unit_id=unit_id)
            units[unit_id] = timestamps[spike_times_in_samples]
            units_valid_times[unit_id] = sort_interval_valid_times
            units_sort_interval[unit_id] = [sort_interval]

        analysis_file_name = AnalysisNwbfile().create(key['nwb_file_name'])
        object_ids = AnalysisNwbfile().add_units(analysis_file_name,
                                                 units, units_valid_times,
                                                 units_sort_interval,
                                                 metrics=metrics, labels=labels)

        if object_ids == '':
            print('Sorting contains no units.'
                  'Created an empty analysis nwb file anyway.')
            units_object_id = ''
        else:
            units_object_id = object_ids[0]

        return analysis_file_name,  units_object_id

    def fetch_nwb(self, *attrs, **kwargs):
        return fetch_nwb(self, (AnalysisNwbfile, 'analysis_file_abs_path'),
                         *attrs, **kwargs)


@schema
class WaveformParameters(dj.Manual):
    definition = """
    waveform_params_name: varchar(80) # name of waveform extraction parameters
    ---
    waveform_params: blob # a dict of waveform extraction parameters
    """

    def insert_default(self):
        waveform_params_name = 'default'
        waveform_params = {'ms_before': 0.5,
                           'ms_after': 0.5,
                           'max_spikes_per_unit': 5000,
                           'n_jobs': 5,
                           'total_memory': '5G'}
        self.insert1([waveform_params_name, waveform_params],
                     skip_duplicates=True)


@schema
class WaveformSelection(dj.Manual):
    definition = """
    -> Curation
    -> WaveformParameters
    ---
    """


@schema
class Waveforms(dj.Computed):
    definition = """
    -> WaveformSelection
    ---
    waveform_extractor_path: varchar(220)
    -> AnalysisNwbfile
    waveforms_object_id: varchar(40)   # Object ID for the waveforms in NWB file
    """

    def make(self, key):
        recording = Curation.get_recording_extractor(key)
        sorting = Curation.get_curated_sorting_extractor(key)

        print('Extracting waveforms...')
        waveform_params = (WaveformParameters & key).fetch1('waveform_params')
        if 'whiten' in waveform_params:
            if waveform_params['whiten']:
                recording = st.preprocessing.whiten(recording)
                # remove the 'whiten' dictionary entry as it is not recognized
                # by spike interface
            del waveform_params['whiten']

        waveform_extractor_name = self._get_waveform_extractor_name(key)
        key['waveform_extractor_path'] = str(
            Path(os.environ['NWB_DATAJOINT_WAVEFORMS_DIR']) /
            Path(waveform_extractor_name))
        if os.path.exists(key['waveform_extractor_path']):
            shutil.rmtree(key['waveform_extractor_path'])
        waveforms = si.extract_waveforms(recording=recording,
                                         sorting=sorting,
                                         folder=key['waveform_extractor_path'],
                                         **waveform_params)

        key['analysis_file_name'] = AnalysisNwbfile().create(key['nwb_file_name'])
        object_id = AnalysisNwbfile().add_units_waveforms(
            key['analysis_file_name'],
            waveform_extractor=waveforms)
        key['waveforms_object_id'] = object_id
        AnalysisNwbfile().add(key['nwb_file_name'], key['analysis_file_name'])

        self.insert1(key)

    def load_waveforms(self, key: dict):
        """Returns a spikeinterface waveform extractor specified by key

        Parameters
        ----------
        key : dict
            Could be an entry in Waveforms, or some other key that uniquely defines
            an entry in Waveforms

        Returns
        -------
        we : spikeinterface.WaveformExtractor
        """
        we_path = (self & key).fetch1('waveform_extractor_path')
        we = si.WaveformExtractor.load_from_folder(we_path)
        return we

    def fetch_nwb(self, key):
        # TODO: implement fetching waveforms from NWB
        return NotImplementedError

    def _get_waveform_extractor_name(self, key):
        we_name = key['curation_id'] + '_waveform'
        return we_name


@schema
class MetricParameters(dj.Manual):
    definition = """
    # Parameters for computing quality metrics of sorted units
    metric_params_name: varchar(200)
    ---
    metric_params: blob
    """
    metric_default_params = {
        'snr': {'peak_sign': 'neg',
                'num_chunks_per_segment': 20,
                'chunk_size': 10000,
                'seed': 0},
        'isi_violation': {'isi_threshold_ms': 1.5,
                          'min_isi_ms': 0.0},
        'nn_isolation': {'max_spikes_for_nn': 1000,
                         'n_neighbors': 5,
                         'n_components': 7,
                         'radius_um': 100,
                         'seed': 0},
        'nn_noise_overlap': {'max_spikes_for_nn': 1000,
                             'n_neighbors': 5,
                             'n_components': 7,
                             'radius_um': 100,
                             'seed': 0},

    }
    available_metrics = list(metric_default_params.keys())

    def get_metric_default_params(self, metric: str):
        "Returns default params for the given metric"
        return self.metric_default_params(metric)

    def insert_default(self):
        self.insert1(
            ['franklab_default', self.metric_default_params], skip_duplicates=True)

    # TODO
    def _validate_metrics_list(self, key):
        """Checks whether a row to be inserted contains only the available metrics
        """
        # get available metrics list
        # get metric list from key
        # compare
        return NotImplementedError


@schema
class MetricSelection(dj.Manual):
    definition = """
    -> Waveforms
    -> MetricParameters
    ---
    """


@schema
class QualityMetrics(dj.Computed):
    definition = """
    -> MetricSelection
    ---
    quality_metrics_path: varchar(500)
    -> AnalysisNwbfile
    object_id: varchar(40) # Object ID for the metrics in NWB file
    """

    def make(self, key):
        waveform_extractor = Waveforms().load_waveforms(key)
        qm = {}
        params = (MetricParameters & key).fetch1('metric_params')
        for metric_name, metric_params in params.items():
            print(metric_params)
            metric = self._compute_metric(
                waveform_extractor, metric_name, **metric_params)
            qm[metric_name] = metric
        qm_name = self._get_quality_metrics_name(key)
        key['quality_metrics_path'] = str(
            Path(os.environ['NWB_DATAJOINT_WAVEFORMS_DIR']) / Path(qm_name + '.json'))
        # save metrics dict as json
        print(f'Computed all metrics: {qm}')
        self._dump_to_json(qm, key['quality_metrics_path'])

        key['analysis_file_name'] = AnalysisNwbfile().create(key['nwb_file_name'])
        key['object_id'] = AnalysisNwbfile().add_units_metrics(
            key['analysis_file_name'], metrics=qm)
        AnalysisNwbfile().add(key['nwb_file_name'], key['analysis_file_name'])

        self.insert1(key)

    def _get_quality_metrics_name(self, key):
        wf_name = Waveforms()._get_waveform_extractor_name(key)
        qm_name = wf_name + '_qm'
        return qm_name

    def _compute_metric(self, waveform_extractor, metric_name, **metric_params):
        metric_func = _metric_name_to_func[metric_name]
        # TODO clean up code below
        if metric_name == 'isi_violation':
            metric = metric_func([], waveform_extractor, **metric_params)
        elif metric_name == 'snr':
            peak_sign = metric_params['peak_sign']
            del metric_params['peak_sign']
            metric = metric_func(waveform_extractor,
                                 peak_sign=peak_sign, **metric_params)
        else:
            metric = {}
            for unit_id in waveform_extractor.sorting.get_unit_ids():
                metric[str(unit_id)] = metric_func(
                    waveform_extractor, this_unit_id=unit_id, **metric_params)
        return metric

    def _dump_to_json(self, qm_dict, save_path):
        new_qm = {}
        for key, value in qm_dict.items():
            m = {}
            for unit_id, metric_val in value.items():
                m[str(unit_id)] = metric_val
            new_qm[str(key)] = m
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(new_qm, f, ensure_ascii=False, indent=4)


def _compute_isi_violation_fractions(self, waveform_extractor, **metric_params):
    print(metric_params)
    isi_threshold_ms = metric_params['isi_threshold_ms']
    min_isi_ms = metric_params['min_isi_ms']

    # Extract the total number of spikes that violated the isi_threshold for each unit
    isi_violation_counts = st.qualitymetrics.compute_isi_violations(
        waveform_extractor, isi_threshold_ms=isi_threshold_ms,
        min_isi_ms=min_isi_ms).isi_violations_count

    # Extract the total number of spikes from each unit
    num_spikes = st.qualitymetrics.compute_num_spikes(waveform_extractor)
    isi_viol_frac_metric = {}
    for unit_id in waveform_extractor.sorting.get_unit_ids():
        isi_viol_frac_metric[str(
            unit_id)] = isi_violation_counts[unit_id] / num_spikes[unit_id]
    return isi_viol_frac_metric


_metric_name_to_func = {
    "snr": st.qualitymetrics.compute_snrs,
    "isi_violation": _compute_isi_violation_fractions,
    'nn_isolation': st.qualitymetrics.nearest_neighbors_isolation,
    'nn_noise_overlap': st.qualitymetrics.nearest_neighbors_noise_overlap
}


@schema
class AutomaticCurationParameters(dj.Manual):
    definition = """
    auto_curation_params_name: varchar(200)   # name of this parameter set
    ---
    merge_params: blob   # params to merge units
    label_params: blob   # params to label units
    """

    def insert_default(self):
        auto_curation_params_name = 'default'
        merge_params = {}
        label_params = {}
        self.insert1([auto_curation_params_name, merge_params,
                     label_params], skip_duplicates=True)


@schema
class AutomaticCurationSelection(dj.Manual):
    definition = """
    -> QualityMetrics
    -> AutomaticCurationParameters
    """


@schema
class AutomaticCuration(dj.Computed):
    definition = """
    -> AutomaticCurationSelection
    ---
    curation_id: varchar(10) # the ID of this sorting
    """

    def make(self, key):
        metrics_path = (QualityMetrics &
                        {'nwb_file_name': key['nwb_file_name'],
                         'recording_id': key['recording_id'],
                         'waveform_params_name': key['waveform_params_name'],
                         'metric_params_name': key['metric_params_name'],
                         'sorting_id': key['parent_sorting_id']}
                        ).fetch1('quality_metrics_path')
        with open(metrics_path) as f:
            quality_metrics = json.load(f)

        # get the curation information and the curated sorting
        parent_curation = (Curation & key).fetch1(as_dict=True)
        parent_merge_groups = parent_curation['merge_groups']
        parent_labels = parent_curation['labels']
        parent_sorting = Curation.get_curated_sorting_extractor(key)

        merge_params = (AutomaticCurationParameters &
                        key).fetch1('merge_params')
        merge_groups, units_merged = self.get_merge_groups(
            parent_sorting, parent_merge_groups, quality_metrics, merge_params)
        if units_merged:
            # get merged sorting extractor here
            return NotImplementedError

        label_params = (AutomaticCurationParameters &
                        key).fetch1('label_params')
        labels = self.get_labels(
            parent_sorting, parent_labels, quality_metrics, label_params)

        # keep the quality metrics only if no merging occurred.
        metrics = quality_metrics if not units_merged else None

        # save the new curation
        curated_sorting_name = self._get_curated_sorting_name(key)
        key['sorting_path'] = str(
            Path(os.getenv('NWB_DATAJOINT_SORTING_DIR')) /
            Path(curated_sorting_name))
        if os.path.exists(key['sorting_path']):
            shutil.rmtree(key['sorting_path'])
        parent_sorting = parent_sorting.save(folder=key['sorting_path'])

        # insert this sorting into the CuratedSpikeSorting Table
        key['sorting_id'] = Curation.insert_curation(
            key, parent_curation_id=key['parent_curation_id'],
            labels=labels, merge_groups=merge_groups, metrics=metrics,
            description='auto curated')
        self.insert1(key)

    def fetch_nwb(self, *attrs, **kwargs):
        return fetch_nwb(self, (AnalysisNwbfile, 'analysis_file_abs_path'),
                         *attrs, **kwargs)

    @staticmethod
    def get_merge_groups(sorting, parent_merge_groups, quality_metrics,
                         merge_params):
        """Identifies units to be merged based on the quality_metrics and
        merge parameters and returns an updated list of merges for the curation

        Parameters
        ----------
        sorting : spike interface sorting object
        parent_merge_groups : list
            Information about previous merges
        quality_metrics : dict
            quality metrics by unit
        merge_params : dict

        Returns
        -------
        sorting : sorting_extractor
        merge_occurred : bool

        """
        # overview:
        # 1. Use quality metrics to determine merge groups for units
        # 2. Combine merge groups with current merge groups to produce union of merges

        if not merge_params:
            return sorting, False
        else:
            # TODO: use the metrics to identify clusters that should be merged
            new_merges = []
            # append these merges to the parent merge_groups
            for m in new_merges:
                # check to see if the first cluster listed is in a current merge group
                found = False
                for idx, pm in enumerate(parent_merge_groups):
                    if m[0] == pm[0]:
                        # add the additional units in m to the identified merge group.
                        pm.extend(m[1:])
                        pm.sort()
                        found = True
                        break
                if not found:
                    # append this merge group to the list
                    parent_merge_groups.append(m)
            return parent_merge_groups.sort()

    @staticmethod
    def get_labels(sorting, parent_labels, quality_metrics, label_params):
        """ Returns a dictionary of labels using quality_metrics and label
        parameters.

        Parameters
        ----------
        sorting : spike interface sorting object
        parent_labels : list
            Previously applied labels
        quality_metrics : dict
            quality metrics by unit
        label_params : dict

        Returns
        -------
        labels : dict

        """
        # overview:
        # 1. Use quality metrics to determine labels for units
        # 2. Append labels to current labels, checking for inconsistencies
        if not label_params:
            return parent_labels
        else:
            new_labels = {}
            for n in new_labels:
                if n in parent_labels:
                    parent_labels[n].extend(new_labels[n])
                    # TODO Implement consistency checks?
                else:
                    parent_labels[n] = new_labels[n]
            return parent_labels


@schema
class FinalizedSpikeSortingSelection(dj.Manual):
    definition = """
    -> Curation
    """


@schema
class FinalizedSpikeSorting(dj.Manual):
    definition = """
    -> FinalizedSpikeSortingSelection
    ---
    -> AnalysisNwbfile
    units_object_id: varchar(40)
    """

    class Unit(dj.Part):
        definition = """
        # Table for holding sorted units
        -> FinalizedSpikeSorting
        unit_id: int   # ID for each unit
        ---
        label='': varchar(200)   # optional set of labels for each unit
        noise_overlap=-1: float   # noise overlap metric for each unit
        isolation_score=-1: float   # isolation score metric for each unit
        isi_violation=-1: float   # ISI violation score for each unit
        snr=0: float            # SNR for each unit
        firing_rate=-1: float   # firing rate
        num_spikes=-1: int   # total number of spikes
        """

    def make(self, key):
        unit_labels_to_remove = ['reject', 'noise']
        # check that the Curation has metrics
        try:
            metrics = (Curation & key).fetch1('metrics')
        except:
            raise Exception(
                f'Metrics for Curation {key} must be calculated before it can be inserted here')
            return

        sorting = Curation.get_curated_sorting_extractor(key)
        unit_ids = sorting.get_unit_ids()
        # Get the labels for the units, add only those units that do not have 'reject' or 'noise' labels
        unit_labels = (Curation & key).fetch1('labels')
        accepted_units = []
        for unit_id in unit_ids:
            if unit_id in unit_labels:
                if len(set(unit_labels_to_remove) & set(unit_labels[unit_id])) == 0:
                    accepted_units.append(unit_id)
            else:
                accepted_units.append(unit_id)

        # get the labels for the accepted units
        labels = {}
        for unit_id in accepted_units:
            labels[unit_id] = ','.join(unit_labels[unit_id])

        # Limit the metrics to accepted units
        metrics = metrics.loc[accepted_units]

        print(f'Found {len(accepted_units)} accepted units')

        # exit out if there are no labels or no accepted units
        if len(unit_labels) == 0 or len(accepted_units) == 0:
            print(f'{key}: no accepted units found')
            return

        # get the sorting and save it in the NWB file
        sorting = Curation.get_curated_sorting_extractor(key)
        recording = Curation.get_recording_extractor(key)

        # get the original units from the Automatic curation NWB file to get the sort interval information
        orig_units = (SpikeSorting & key).fetch_nwb()[0]['units']
        orig_units = orig_units.loc[accepted_units]
        # TODO: fix if unit 0 doesn't exist
        sort_interval = orig_units.iloc[0]['sort_interval']
        sort_interval_list_name = (SpikeSortingRecording & key).fetch1(
            'sort_interval_list_name')

        timestamps = SpikeSortingRecording._get_recording_timestamps(recording)

        (key['analysis_file_name'],
         key['units_object_id']) = Curation.save_sorting_nwb(
            self, key, sorting, timestamps, sort_interval_list_name,
            sort_interval, metrics=metrics, unit_ids=accepted_units)
        self.insert1(key)

        # now add the units
        # Remove the non primary key entries.
        del key['units_object_id']
        del key['analysis_file_name']

        metric_fields = self.metrics_fields()

        for unit_id in accepted_units:
            key['unit_id'] = unit_id
            key['label'] = labels[unit_id]
            for field in metric_fields:
                if field in metrics[unit_id]:
                    key[field] = metrics[unit_id][field]
                else:
                    WarningMessage(
                        f'No metric named {field} in metrics table; skipping')
        self.Unit.insert1(key)

    def metrics_fields(self):
        """Returns a list of the metrics that are currently in the Units table
        """
        unit_fields = list(self.Unit().fetch(limit=1, as_dict=True)[0].keys())
        parent_fields = list(FinalizedSpikeSorting.fetch(
            limit=1, as_dict=True)[0].keys())
        parent_fields.extend(['unit_id', 'label'])
        return [field for field in unit_fields if field not in parent_fields]

    def fetch_nwb(self, *attrs, **kwargs):
        return fetch_nwb(
            self, (AnalysisNwbfile, 'analysis_file_abs_path'), *attrs, **kwargs)
