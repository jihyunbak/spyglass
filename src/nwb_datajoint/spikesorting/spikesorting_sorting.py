from ..common.common_nwbfile import AnalysisNwbfile
from ..common.common_lab import LabTeam, LabMember
from ..common.dj_helper_fn import fetch_nwb
from .spikesorting_artifact import ArtifactRemovedIntervalList
from .spikesorting_recording import SpikeSortingRecording, SpikeSortingRecordingSelection

import os
import numpy as np
import datajoint as dj
from pathlib import Path
import time
import shutil
import tempfile

import spikeinterface as si
import spikeinterface.sorters as sis
import spikeinterface.toolkit as sit

schema = dj.schema('spikesorting_sorting')

@schema
class SpikeSorterParameters(dj.Manual):
    definition = """
    sorter: varchar(200)
    sorter_params_name: varchar(200)
    ---
    sorter_params: blob
    """

    def insert_default(self):
        """Default params from spike sorters available via spikeinterface
        """
        sorters = sis.available_sorters()
        for sorter in sorters:
            sorter_params = sis.get_default_params(sorter)
            self.insert1([sorter, 'default', sorter_params],
                         skip_duplicates=True)

        # Insert Frank lab defaults
        sorter = "mountainsort4"
        
        # Hippocampus tetrode default
        sorter_params_name = "franklab_tetrode_hippocampus_30KHz"
        sorter_params = {'detect_sign': -1,
                         'adjacency_radius': -1,
                         'freq_min': 600,
                         'freq_max': 6000,
                         'filter': False,
                         'whiten': True,
                         'num_workers': 1,
                         'clip_size': 40,
                         'detect_threshold': 3,
                         'detect_interval': 10}
        self.insert1([sorter, sorter_params_name, sorter_params],
                     skip_duplicates=True)
        
        # Cortical probe default
        sorter_params_name = "franklab_probe_ctx_30KHz"
        sorter_params = {'detect_sign': -1,
                         'adjacency_radius': 100,
                         'freq_min': 300,
                         'freq_max': 6000,
                         'filter': False,
                         'whiten': True,
                         'num_workers': 1,
                         'clip_size': 40,
                         'detect_threshold': 3,
                         'detect_interval': 10}
        self.insert1([sorter, sorter_params_name, sorter_params],
                     skip_duplicates=True)

@schema
class SpikeSortingSelection(dj.Manual):
    definition = """
    # Table for holding selection of recording and parameters for each spike sorting run
    -> SpikeSortingRecording
    -> SpikeSorterParameters
    -> ArtifactRemovedIntervalList
    ---
    import_path = "": varchar(200)  # optional path to previous curated sorting output
    """


@schema
class SpikeSorting(dj.Computed):
    definition = """
    -> SpikeSortingSelection
    ---
    sorting_path: varchar(1000)
    time_of_sort: int   # in Unix time, to the nearest second
    """

    def make(self, key: dict):
        """Runs spike sorting on the data and parameters specified by the
        SpikeSortingSelection table and inserts a new entry to SpikeSorting table.
        Specifically,
        1. Loads saved recording and runs the sort on it with spikeinterface
        2. Saves the sorting with spikeinterface
        3. Creates an analysis NWB file and saves the sorting there
           (this is redundant with 2; will change in the future)
        """

        recording_path = (SpikeSortingRecording & key).fetch1('recording_path')
        recording = si.load_extractor(recording_path)

        timestamps = SpikeSortingRecording._get_recording_timestamps(recording)

        # load valid times
        artifact_times = (ArtifactRemovedIntervalList &
                          key).fetch1('artifact_times')
        if artifact_times.ndim == 1:
            artifact_times = np.expand_dims(artifact_times, 0)

        if artifact_times:
            # convert valid intervals to indices
            list_triggers = []
            for interval in artifact_times:
                list_triggers.append(np.arange(np.searchsorted(timestamps, interval[0]),
                                               np.searchsorted(timestamps, interval[1])))
            list_triggers = np.asarray(list_triggers).flatten().tolist()

            if recording.get_num_segments() > 1:
                recording = si.concatenate_recordings(recording.recording_list)
            recording = sit.remove_artifacts(recording=recording, list_triggers=list_triggers,
                                            ms_before=0, ms_after=0, mode='zeros')

        print(f'Running spike sorting on {key}...')
        sorter, sorter_params = (SpikeSorterParameters & key).fetch1(
            'sorter', 'sorter_params')
        
        sorter_temp_dir = tempfile.TemporaryDirectory(dir=os.getenv('NWB_DATAJOINT_TEMP_DIR'))
        
        sorting = sis.run_sorter(sorter, recording,
                                output_folder=sorter_temp_dir.name,
                                delete_output_folder=True,
                                **sorter_params)
        key['time_of_sort'] = int(time.time())

        print('Saving sorting results...')
        sorting_folder = Path(os.getenv('NWB_DATAJOINT_SORTING_DIR'))
        sorting_name = self._get_sorting_name(key)
        key['sorting_path'] = str(sorting_folder / Path(sorting_name))
        if os.path.exists(key['sorting_path']):
            shutil.rmtree(key['sorting_path'])
        sorting = sorting.save(folder=key['sorting_path'])
        self.insert1(key)

    def delete(self):
        """Extends the delete method of base class to implement permission checking.
        Note that this is NOT a security feature, as anyone that has access to source code
        can disable it; it just makes it less likely to accidentally delete entries.
        """
        current_user_name = dj.config['database.user']
        entries = self.fetch()
        permission_bool = np.zeros((len(entries),))
        print(
            f'Attempting to delete {len(entries)} entries, checking permission...')

        for entry_idx in range(len(entries)):
            # check the team name for the entry, then look up the members in that team, then get their datajoint user names
            team_name = (SpikeSortingRecordingSelection & (
                SpikeSortingRecordingSelection & entries[entry_idx]).proj()).fetch1()['team_name']
            lab_member_name_list = (LabTeam.LabTeamMember & {
                                    'team_name': team_name}).fetch('lab_member_name')
            datajoint_user_names = []
            for lab_member_name in lab_member_name_list:
                datajoint_user_names.append((LabMember.LabMemberInfo & {
                                            'lab_member_name': lab_member_name}).fetch1('datajoint_user_name'))
            permission_bool[entry_idx] = current_user_name in datajoint_user_names
        if np.sum(permission_bool) == len(entries):
            print('Permission to delete all specified entries granted.')
            super().delete()
        else:
            raise Exception(
                'You do not have permission to delete all specified entries. Not deleting anything.')

    def fetch_nwb(self, *attrs, **kwargs):
        return fetch_nwb(self, (AnalysisNwbfile, 'analysis_file_abs_path'), *attrs, **kwargs)

    def nightly_cleanup(self):
        """Clean up spike sorting directories that are not in the SpikeSorting table.
        This should be run after AnalysisNwbFile().nightly_cleanup()
        """
        # get a list of the files in the spike sorting storage directory
        dir_names = next(os.walk(os.environ['NWB_DATAJOINT_SORTING_DIR']))[1]
        # now retrieve a list of the currently used analysis nwb files
        analysis_file_names = self.fetch('analysis_file_name')
        for dir in dir_names:
            if not dir in analysis_file_names:
                full_path = str(Path(
                    os.environ['NWB_DATAJOINT_SORTING_DIR']) / dir)
                print(f'removing {full_path}')
                shutil.rmtree(
                    str(Path(os.environ['NWB_DATAJOINT_SORTING_DIR']) / dir))

    def _get_sorting_name(self, key):
        recording_name = SpikeSortingRecording._get_recording_name(key)
        sorting_name = recording_name + '_' \
            + key['sorter'] + '_' \
            + key['sorter_params_name'] + '_' \
            + key['artifact_removed_interval_list_name']
        return sorting_name

    # TODO: write a function to import sortings done outside of dj

    def _import_sorting(self, key):
        raise NotImplementedError
