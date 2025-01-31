#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import argparse
import codecs
from datetime import datetime
import io
import json
import os
import shutil
import sys

from difflib import unified_diff  # python's own diff library
from multiprocessing import Process, Queue, freeze_support  # add stuff to either make series calls, or multi-threading

from epregressions.diffs import math_diff, table_diff, thresh_dict as td
from epregressions import energyplus
from epregressions.structures import (
    ForceRunType,
    TextDifferences,
    EndErrSummary,
    MathDifferences,
    TableDifferences,
    CompletedStructure,
    ReportingFreq,
    TestEntry
)


# get the current file path for convenience
path = os.path.dirname(__file__)
script_dir = os.path.abspath(path)


class TestRunConfiguration:
    def __init__(self, force_run_type, num_threads, report_freq, build_a, build_b, single_test_run=False):
        self.force_run_type = force_run_type
        self.TestOneFile = single_test_run
        self.num_threads = num_threads
        self.buildA = build_a
        self.buildB = build_b
        self.report_freq = report_freq


class TestCaseCompleted:
    def __init__(self, run_directory, case_name, run_status, error_msg_reported_already, name_of_thread):
        self.run_directory = run_directory
        self.case_name = case_name
        self.run_success = run_status
        self.name_of_thread = name_of_thread
        self.muffle_err_msg = error_msg_reported_already


# the actual main test suite run class
class SuiteRunner:

    def __init__(self, run_config, these_entries):

        # initialize callbacks
        self.print_callback = None
        self.starting_callback = None
        self.case_completed_callback = None
        self.simulations_complete_callback = None
        self.diff_completed_callback = None
        self.all_done_callback = None
        self.cancel_callback = None
        self.id_like_to_stop_now = False

        # User configuration; read from the run_configuration
        self.force_run_type = run_config.force_run_type
        self.TestOneFile = run_config.TestOneFile
        self.number_of_threads = int(run_config.num_threads)
        self.min_reporting_freq = run_config.report_freq

        # File list brought in separately
        self.entries = these_entries

        # Main test configuration here
        self.build_tree_a = run_config.buildA.get_build_tree()
        self.run_case_a = run_config.buildA.run
        self.build_tree_b = run_config.buildB.get_build_tree()
        self.run_case_b = run_config.buildB.run

        # Settings/paths defined relative to this script
        self.path_to_file_list = os.path.join(script_dir, "files_to_run.txt")
        self.thresh_dict_file = os.path.join(script_dir, 'diffs', "math_diff.config")
        self.math_diff_executable = os.path.join(script_dir, "math_diff.py")
        self.table_diff_executable = os.path.join(script_dir, "table_diff.py")

        # Settings/paths defined relative to the buildA/buildB test directories
        # the tests directory will be different based on forceRunType
        if self.force_run_type == ForceRunType.ANNUAL:
            self.test_output_dir = "Tests-Annual"
        elif self.force_run_type == ForceRunType.DD:
            self.test_output_dir = "Tests-DDOnly"
        elif self.force_run_type == ForceRunType.NONE:
            self.test_output_dir = "Tests"
        i = datetime.now()
        self.test_output_dir += i.strftime('_%Y%m%d_%H%M%S')

        # Filename specification, not path specific
        self.ep_in_filename = "in.idf"

        # For files that don't have a specified weather file, use Chicago
        self.default_weather_filename = "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"

        # Required to avoid stalls
        if self.number_of_threads == 1:
            freeze_support()

    def run_test_suite(self):

        # reset this flag
        self.id_like_to_stop_now = False

        # do some preparation
        self.prepare_dir_structure(self.build_tree_a, self.build_tree_b, self.test_output_dir)

        if self.id_like_to_stop_now:  # pragma: no cover
            self.my_cancelled()
            return

        num_builds = 2
        self.my_starting(num_builds, len(self.entries))

        # run the energyplus script
        if self.run_case_a:
            self.run_build(self.build_tree_a)
            if self.id_like_to_stop_now:  # pragma: no cover
                self.my_cancelled()
                return
        if self.run_case_b:
            self.run_build(self.build_tree_b)
            if self.id_like_to_stop_now:  # pragma: no cover
                self.my_cancelled()
                return
        self.my_simulationscomplete()

        response = self.diff_logs_for_build()

        try:
            self.my_print('Writing runtime summary file')
            csv_file_path = os.path.join(self.build_tree_a['build_dir'], self.test_output_dir, 'run_times.csv')
            response.to_runtime_summary(csv_file_path)
            self.my_print('Runtime summary written successfully')
        except Exception as this_exception:  # pragma: no cover
            self.my_print('Could not write runtime summary file: ' + str(this_exception))

        try:
            self.my_print('Writing simulation results summary file')
            json_file_path = os.path.join(self.build_tree_a['build_dir'], self.test_output_dir, 'test_results.json')
            response.to_json_summary(json_file_path)
            self.my_print('Results summary written successfully')
        except Exception as this_exception:  # pragma: no cover
            self.my_print('Could not write results summary file: ' + str(this_exception))

        self.my_print("Test suite complete for directories:")
        self.my_print("\t%s" % self.build_tree_a['build_dir'])
        self.my_print("\t%s" % self.build_tree_b['build_dir'])
        self.my_print("Test suite complete")

        self.my_alldone(response)
        return response

    def prepare_dir_structure(self, b_a, b_b, d_test):

        # make tests directory as needed
        if b_a:
            if not os.path.exists(os.path.join(b_a['build_dir'], d_test)):
                os.mkdir(os.path.join(b_a['build_dir'], d_test))
        if b_b:
            if not os.path.exists(os.path.join(b_b['build_dir'], d_test)):
                os.mkdir(os.path.join(b_b['build_dir'], d_test))
        self.my_print('Created test directories at <build-dir>/%s' % d_test)

    @staticmethod
    def read_file_content(file_path):
        with codecs.open(file_path, encoding='utf-8', errors='ignore') as f_idf:
            idf_text = f_idf.read()
        return idf_text

    def run_build(self, build_tree):

        this_test_dir = self.test_output_dir
        local_run_type = self.force_run_type

        # Create queues for threaded operation
        task_queue = Queue()
        done_queue = Queue()

        # Create a job list
        energy_plus_runs = []

        # loop over all entries
        for this_entry in self.entries:

            # first remove the previous test directory for this file and rename it
            test_run_directory = os.path.join(build_tree['build_dir'], this_test_dir, this_entry.basename)
            if os.path.exists(test_run_directory):  # pragma: no cover - dir name is generated by local timestamp now
                shutil.rmtree(test_run_directory)
            os.mkdir(test_run_directory)

            # establish the absolute path to the idf or imf, and append .idf or .imf as necessary
            idf_base = os.path.join(build_tree['test_files_dir'], this_entry.basename)
            idf_base = idf_base.strip()
            idf_path = idf_base + ".idf"
            imf_path = idf_base + ".imf"

            parametric_file = False
            if os.path.exists(idf_path):

                # copy the idf into the test directory, renaming to in.idf
                shutil.copy(idf_path, os.path.join(test_run_directory, self.ep_in_filename))

                # read in the entire text of the idf to do some special operations;
                # could put in one line, but the with block ensures the file handle is closed
                idf_text = SuiteRunner.read_file_content(os.path.join(test_run_directory, self.ep_in_filename))

                # if the file requires the window 5 data set file, bring it into the test run directory
                if 'Window5DataFile.dat' in idf_text:
                    os.mkdir(os.path.join(test_run_directory, 'datasets'))
                    shutil.copy(os.path.join(build_tree['data_sets_dir'], 'Window5DataFile.dat'),
                                os.path.join(test_run_directory, 'datasets'))
                    idf_text = idf_text.replace('..\\datasets\\Window5DataFile.dat', 'datasets/Window5DataFile.dat')

                # if the file requires the TDV data set file, bring it
                #  into the test run directory, right now I think it's broken
                if 'DataSets\\TDV' in idf_text or 'DataSets\\\\TDV' in idf_text:
                    os.mkdir(os.path.join(test_run_directory, 'datasets'))
                    os.mkdir(os.path.join(test_run_directory, 'datasets', 'TDV'))
                    tdv_dir = os.path.join(build_tree['data_sets_dir'], 'TDV')
                    src_files = os.listdir(tdv_dir)
                    for file_name in src_files:
                        full_file_name = os.path.join(tdv_dir, file_name)
                        if os.path.isfile(full_file_name):
                            shutil.copy(
                                full_file_name,
                                os.path.join(test_run_directory, 'datasets', 'TDV')
                            )
                    idf_text = idf_text.replace(
                        '..\\datasets\\TDV\\TDV_2008_kBtu_CTZ06.csv',
                        os.path.join('datasets', 'TDV', 'TDV_2008_kBtu_CTZ06.csv')
                    )

                if 'HybridZoneModel_TemperatureData.csv' in idf_text:
                    shutil.copy(
                        os.path.join(build_tree['test_files_dir'], 'HybridZoneModel_TemperatureData.csv'),
                        os.path.join(test_run_directory, 'HybridZoneModel_TemperatureData.csv')
                    )

                if 'report variable dictionary' in idf_text:
                    idf_text = idf_text.replace('report variable dictionary', '')

                if 'Parametric:' in idf_text:
                    parametric_file = True

                # if the file requires the FMUs data set file, bring it
                #  into the test run directory, right now I think it's broken
                if 'ExternalInterface:' in idf_text:
                    self.my_print('Skipping an FMU based file as this is not set up to run yet')
                    continue
                    # os.mkdir(os.path.join(test_run_directory, 'datasets'))
                    # os.mkdir(os.path.join(test_run_directory, 'datasets', 'FMUs'))
                    # source_dir = os.path.join('datasets', 'FMUs')
                    # src_files = os.listdir(source_dir)
                    # for file_name in src_files:
                    #     full_file_name = os.path.join(source_dir, file_name)
                    #     if os.path.isfile(full_file_name):
                    #         shutil.copy(
                    #             full_file_name,
                    #             os.path.join(test_run_directory, 'datasets', 'FMUs')
                    #         )

                # rewrite the idf with the (potentially) modified idf text
                with io.open(
                    os.path.join(build_tree['build_dir'], this_test_dir, this_entry.basename, self.ep_in_filename),
                    'w',
                    encoding='utf-8'
                ) as f_i:
                    f_i.write("%s\n" % idf_text)

            elif os.path.exists(imf_path):

                shutil.copy(
                    imf_path, os.path.join(build_tree['build_dir'], this_test_dir, this_entry.basename, 'in.imf')
                )
                # find the rest of the imf files and copy them into the test directory
                source_files = os.listdir(build_tree['test_files_dir'])
                for file_name in source_files:
                    if file_name[-4:] == '.imf':
                        full_file_name = os.path.join(build_tree['test_files_dir'], file_name)
                        shutil.copy(
                            full_file_name, os.path.join(build_tree['build_dir'], this_test_dir, this_entry.basename)
                        )

            else:

                # if the file doesn't exist, just move along
                self.my_print("Input file doesn't exist in either idf or imf form:")
                self.my_print("   IDF: %s" % idf_path)
                self.my_print("   IMF: %s" % imf_path)
                self.my_casecompleted(TestCaseCompleted(this_test_dir, this_entry.basename, False, False, ""))
                continue

            rvi = os.path.join(build_tree['test_files_dir'], this_entry.basename) + '.rvi'
            if os.path.exists(rvi):
                shutil.copy(rvi, os.path.join(build_tree['build_dir'], this_test_dir, this_entry.basename, 'in.rvi'))

            mvi = os.path.join(build_tree['test_files_dir'], this_entry.basename) + '.mvi'
            if os.path.exists(mvi):
                shutil.copy(mvi, os.path.join(build_tree['build_dir'], this_test_dir, this_entry.basename, 'in.mvi'))

            epw_path = os.path.join(build_tree['source_dir'], 'weather', self.default_weather_filename)
            if this_entry.epw:
                epw_path = os.path.join(build_tree['weather_dir'], this_entry.epw + '.epw')
                epw_exists = os.path.exists(epw_path)
                if not epw_exists:
                    self.my_print(
                        "For case %s, weather file did not exist at %s, using a default one!" % (
                            this_entry.basename, epw_path
                        )
                    )
                    epw_path = os.path.join(build_tree['source_dir'], 'weather', self.default_weather_filename)

            energy_plus_runs.append(
                (
                    energyplus.execute_energyplus,
                    (
                        build_tree,
                        this_entry.basename,
                        test_run_directory,
                        local_run_type,
                        self.min_reporting_freq,
                        parametric_file,
                        epw_path
                    )
                )
            )

        if self.number_of_threads == 1:
            for task in energy_plus_runs:
                # Sometime I'll look at how to squash the args down, for now just fill a temp array as needed
                tmp_array = []
                for val in task[1]:
                    tmp_array.append(val)
                if self.id_like_to_stop_now:  # pragma: no cover
                    return  # self.my_cancelled() is called in parent function
                ret = energyplus.execute_energyplus(*tmp_array)
                self.my_casecompleted(TestCaseCompleted(ret[0], ret[1], ret[2], ret[3], ret[4]))
        else:
            # Submit tasks
            for task in energy_plus_runs:
                task_queue.put(task)

            # Start worker processes
            for i in range(self.number_of_threads):
                p = Process(target=self.threaded_worker, args=(task_queue, done_queue))
                p.daemon = True  # this *is* "necessary" to allow cancelling the suite
                p.start()

            # Get and print results
            for i in range(len(energy_plus_runs)):
                ret = done_queue.get()
                self.my_casecompleted(TestCaseCompleted(ret[0], ret[1], ret[2], ret[3], ret[4]))

            # Tell child processes to stop
            for i in range(self.number_of_threads):
                task_queue.put('STOP')

    def threaded_worker(self, input_data, output):  # pragma: no cover - even with multiprocess, coverage misses this
        for func, these_args in iter(input_data.get, 'STOP'):
            if self.id_like_to_stop_now:
                print("I'd like to stop now.")
                return
            return_val = func(*these_args)
            output.put(return_val)  # something needs to be put into the output queue for everything to work

    @staticmethod
    def both_files_exist(base_path_a, base_path_b, common_relative_path):
        if os.path.exists(os.path.join(base_path_a, common_relative_path)):
            if os.path.exists(os.path.join(base_path_b, common_relative_path)):
                return True
        return False

    @staticmethod
    def diff_text_files(file_a, file_b, diff_file):
        # read the contents of the two files into a list, could read it into text first
        with io.open(file_a, encoding='utf-8') as f_txt_1:
            txt1 = f_txt_1.readlines()
        with io.open(file_b, encoding='utf-8') as f_txt_2:
            txt2 = f_txt_2.readlines()
        # remove any lines that have some specific listed strings in them
        txt1_cleaned = []
        skip_strings = [
            "Program Version,EnergyPlus",
            "EnergyPlus Completed",
            "EnergyPlus Terminated",
            "DElight input generated",
            "(idf)=",
            "(user input)=",
            "(input file)="
        ]
        for line in txt1:
            if any([x in line for x in skip_strings]):
                pass
            else:
                txt1_cleaned.append(line)
        txt2_cleaned = []
        for line in txt2:
            if any([x in line for x in skip_strings]):
                pass
            else:
                txt2_cleaned.append(line)
        # compare for equality, if it is faster to compare strings then lists, may want to refactor
        if txt1_cleaned == txt2_cleaned:
            return TextDifferences.EQUAL
        # if we aren't equal, compute the comparison and write to the output file, return that diffs occurred
        comparison = unified_diff(txt1_cleaned, txt2_cleaned)
        out_file = io.open(diff_file, 'w', encoding='utf-8')
        out_lines = list(comparison)
        for out_line in out_lines:
            if sys.version_info[0] == 2:
                out_line = out_line.encode('ascii', 'ignore').decode('ascii')  # pragma: no cover
            out_file.write(out_line)
        out_file.close()
        return TextDifferences.DIFFS

    def process_diffs_for_one_case(self, this_entry, ci_mode=False):

        if ci_mode:  # in "ci_mode" the build directory is actually the output directory of each file
            case_result_dir_1 = self.build_tree_a['build_dir']
            case_result_dir_2 = self.build_tree_b['build_dir']
        else:
            case_result_dir_1 = os.path.join(
                self.build_tree_a['build_dir'], self.test_output_dir, this_entry.basename
            )
            case_result_dir_2 = os.path.join(
                self.build_tree_b['build_dir'], self.test_output_dir, this_entry.basename
            )

        out_dir = case_result_dir_1

        # we aren't using math_diff and table_diffs summary csv files, so use blanks
        path_to_math_diff_log = ""
        path_to_table_diff_log = ""

        # shortcut
        join = os.path.join

        # process the end files first
        status_case1 = EndErrSummary.STATUS_MISSING
        status_case2 = EndErrSummary.STATUS_MISSING
        runtime_case1 = 0
        runtime_case2 = 0
        end_path = join(case_result_dir_1, 'eplusout.end')
        if os.path.exists(end_path):
            [status_case1, runtime_case1] = self.process_end_file(end_path)
        end_path = join(case_result_dir_2, 'eplusout.end')
        if os.path.exists(end_path):
            [status_case2, runtime_case2] = self.process_end_file(end_path)

        # one quick check here for expect-fatal tests
        if this_entry.basename == 'EMSTestMathAndKill':
            if status_case1 == EndErrSummary.STATUS_FATAL and status_case2 == EndErrSummary.STATUS_FATAL:
                # this is actually what we expect, so add a success result, print a message, and get out
                this_entry.add_summary_result(
                    EndErrSummary(
                        EndErrSummary.STATUS_SUCCESS,
                        runtime_case1,
                        EndErrSummary.STATUS_SUCCESS,
                        runtime_case2
                    ))
                self.my_print("EMSTestMathAndKill Fatal-ed as expected, continuing with no diff checking on it")
                return this_entry

        # add the initial end/err summary to the entry
        this_entry.add_summary_result(EndErrSummary(status_case1, runtime_case1, status_case2, runtime_case2))

        # Handle the results of the end file before doing anything with diffs
        # Case 1: Both end files existed, so E+ did complete
        if not any(x == EndErrSummary.STATUS_MISSING for x in [status_case1, status_case2]):
            # Case 1a: Both files are successful
            if sum(x == EndErrSummary.STATUS_SUCCESS for x in [status_case1, status_case2]) == 2:
                # Just continue to process diffs
                self.my_print(
                    "Processing (Diffs) : %s" % this_entry.basename
                )
            # Case 1b: Both completed, but both failed: report that it failed in both cases and return early
            elif sum(x == EndErrSummary.STATUS_SUCCESS for x in [status_case1, status_case2]) == 0:
                self.my_print(
                    "Skipping entry because it has a fatal error in both base and mod cases: %s" % this_entry.basename
                )
                return this_entry
            # Case 1c: Both completed, but one failed: report that it failed in one case and return early
            elif sum(x == EndErrSummary.STATUS_SUCCESS for x in [status_case1, status_case2]) == 1:
                self.my_print(
                    "Skipping an entry because it appears to have a fatal error in one case: %s" % this_entry.basename
                )
                return this_entry
        # Case 2: Both end files DID NOT exist
        elif all(x == EndErrSummary.STATUS_MISSING for x in [status_case1, status_case2]):
            self.my_print(
                "Skipping entry because it failed (crashed) in both base and mod cases: %s" % this_entry.basename
            )
            return this_entry
        # Case 3: Both end files DID NOT exist
        elif sum(x == EndErrSummary.STATUS_MISSING for x in [status_case1, status_case2]) == 1:
            self.my_print(
                "Skipping an entry because it appears to have failed (crashed) in one case: %s" % this_entry.basename
            )
            return this_entry
        # Case 4: Unhandled combination
        else:  # pragma: no cover -- I don't think we can get here
            self.my_print(
                "Skipping an entry because it has an unknown end status: %s" % this_entry.basename
            )
            return this_entry

        # Load diffing threshold dictionary
        thresh_dict = td.ThreshDict(self.thresh_dict_file)

        # Do Math (CSV) Diffs
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.csv'):
            this_entry.add_math_differences(MathDifferences(math_diff.math_diff(
                thresh_dict,
                join(case_result_dir_1, 'eplusout.csv'),
                join(case_result_dir_2, 'eplusout.csv'),
                join(out_dir, 'eplusout.csv.absdiff.csv'),
                join(out_dir, 'eplusout.csv.percdiff.csv'),
                join(out_dir, 'eplusout.csv.diffsummary.csv'),
                path_to_math_diff_log)), MathDifferences.ESO)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusmtr.csv'):
            this_entry.add_math_differences(MathDifferences(math_diff.math_diff(
                thresh_dict,
                join(case_result_dir_1, 'eplusmtr.csv'),
                join(case_result_dir_2, 'eplusmtr.csv'),
                join(out_dir, 'eplusmtr.csv.absdiff.csv'),
                join(out_dir, 'eplusmtr.csv.percdiff.csv'),
                join(out_dir, 'eplusmtr.csv.diffsummary.csv'),
                path_to_math_diff_log)), MathDifferences.MTR)

        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'epluszsz.csv'):
            this_entry.add_math_differences(MathDifferences(math_diff.math_diff(
                thresh_dict,
                join(case_result_dir_1, 'epluszsz.csv'),
                join(case_result_dir_2, 'epluszsz.csv'),
                join(out_dir, 'epluszsz.csv.absdiff.csv'),
                join(out_dir, 'epluszsz.csv.percdiff.csv'),
                join(out_dir, 'epluszsz.csv.diffsummary.csv'),
                path_to_math_diff_log)), MathDifferences.ZSZ)

        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusssz.csv'):
            this_entry.add_math_differences(MathDifferences(math_diff.math_diff(
                thresh_dict,
                join(case_result_dir_1, 'eplusssz.csv'),
                join(case_result_dir_2, 'eplusssz.csv'),
                join(out_dir, 'eplusssz.csv.absdiff.csv'),
                join(out_dir, 'eplusssz.csv.percdiff.csv'),
                join(out_dir, 'eplusssz.csv.diffsummary.csv'),
                path_to_math_diff_log)), MathDifferences.SSZ)

        # Do Tabular (HTML) Diffs
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplustbl.htm'):
            this_entry.add_table_differences(TableDifferences(table_diff.table_diff(
                thresh_dict,
                join(case_result_dir_1, 'eplustbl.htm'),
                join(case_result_dir_2, 'eplustbl.htm'),
                join(out_dir, 'eplustbl.htm.absdiff.htm'),
                join(out_dir, 'eplustbl.htm.percdiff.htm'),
                join(out_dir, 'eplustbl.htm.summarydiff.htm'),
                path_to_table_diff_log)))

        # Do Textual Diffs
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.audit'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.audit'),
                join(case_result_dir_2, 'eplusout.audit'),
                join(out_dir, 'eplusout.audit.diff'))), TextDifferences.AUD)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.bnd'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.bnd'),
                join(case_result_dir_2, 'eplusout.bnd'),
                join(out_dir, 'eplusout.bnd.diff'))), TextDifferences.BND)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.dxf'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.dxf'),
                join(case_result_dir_2, 'eplusout.dxf'),
                join(out_dir, 'eplusout.dxf.diff'))), TextDifferences.DXF)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.eio'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.eio'),
                join(case_result_dir_2, 'eplusout.eio'),
                join(out_dir, 'eplusout.eio.diff'))), TextDifferences.EIO)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.mdd'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.mdd'),
                join(case_result_dir_2, 'eplusout.mdd'),
                join(out_dir, 'eplusout.mdd.diff'))), TextDifferences.MDD)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.mtd'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.mtd'),
                join(case_result_dir_2, 'eplusout.mtd'),
                join(out_dir, 'eplusout.mtd.diff'))), TextDifferences.MTD)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.rdd'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.rdd'),
                join(case_result_dir_2, 'eplusout.rdd'),
                join(out_dir, 'eplusout.rdd.diff'))), TextDifferences.RDD)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.shd'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.shd'),
                join(case_result_dir_2, 'eplusout.shd'),
                join(out_dir, 'eplusout.shd.diff'))), TextDifferences.SHD)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.err'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.err'),
                join(case_result_dir_2, 'eplusout.err'),
                join(out_dir, 'eplusout.err.diff'))), TextDifferences.ERR)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.delightin'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.delightin'),
                join(case_result_dir_2, 'eplusout.delightin'),
                join(out_dir, 'eplusout.delightin.diff'))), TextDifferences.DL_IN)
        if self.both_files_exist(case_result_dir_1, case_result_dir_2, 'eplusout.delightout'):
            this_entry.add_text_differences(TextDifferences(self.diff_text_files(
                join(case_result_dir_1, 'eplusout.delightout'),
                join(case_result_dir_2, 'eplusout.delightout'),
                join(out_dir, 'eplusout.delightout.diff'))), TextDifferences.DL_OUT)

        # return the updated entry
        return this_entry

    @staticmethod
    def process_end_file(end_path):

        # The end file contains enough info to determine the simulation completion status and runtime
        # success:
        #     EnergyPlus Completed Successfully-- 1 Warning; 0 Severe Errors; Elapsed Time=00hr 00min  1.42sec
        # fatal:
        #     EnergyPlus Terminated--Fatal Error Detected. 0 Warning; 4 Severe Errors; Elapse
        #      d Time=00hr 00min  0.59sec
        # A NEWLINE?? Gotta sanitize it.
        with io.open(end_path, encoding='utf-8') as f_end:
            end_contents = f_end.read().replace("\n", "")

        if "Successfully" in end_contents:
            status = EndErrSummary.STATUS_SUCCESS
        elif "Fatal" in end_contents:
            status = EndErrSummary.STATUS_FATAL
        else:
            return [EndErrSummary.STATUS_UNKNOWN, 0]

        # now process the time string, which is located after a singular equals sign, in the form: 00hr 00min  2.80sec
        # hours and minutes are fixed to 2 decimal points...not sure what happens if it takes over a day...
        # seconds is a floating point that can have 1 or 2 digits before the decimal
        time_string = end_contents.split('=')[1]
        time_string_tokens = time_string.split(' ')
        # remove any blank entries due to duplicated tokens
        time_string_tokens = [x for x in time_string_tokens if x]
        hours = float(time_string_tokens[0][0:2])
        minutes = float(time_string_tokens[1][0:2])
        seconds_term = time_string_tokens[2]
        seconds_index = seconds_term.index('s')
        seconds = float(seconds_term[0:(seconds_index - 1)])
        total_runtime_seconds = hours * 3600.0 + minutes * 60.0 + seconds

        # return results from this end file
        return [status, total_runtime_seconds]

    # diff_logs_for_build creates diff logs between simulations in two build directories
    def diff_logs_for_build(self):

        completed_structure = CompletedStructure(
            self.build_tree_a['source_dir'], self.build_tree_a['build_dir'],
            self.build_tree_b['source_dir'], self.build_tree_b['build_dir'],
            os.path.join(self.build_tree_a['build_dir'], self.test_output_dir)
        )
        for this_entry in self.entries:
            try:
                this_entry = self.process_diffs_for_one_case(this_entry)
                completed_structure.add_test_entry(this_entry)
            except Exception as e:  # pragma: no cover -- I'm not trying to catch every possible case here
                self.my_print(
                    (
                        "Unexpected error processing diffs for %s, could indicate an E+ crash caused corrupted files"
                    ) % this_entry.basename
                )
                self.my_print("Message: %s" % e)
            finally:
                self.my_diffcompleted(this_entry.basename)
        return completed_structure

    def add_callbacks(self, print_callback, simstarting_callback, casecompleted_callback, simulationscomplete_callback,
                      diffcompleted_callback, alldone_callback, cancel_callback):
        self.print_callback = print_callback
        self.starting_callback = simstarting_callback
        self.case_completed_callback = casecompleted_callback
        self.simulations_complete_callback = simulationscomplete_callback
        self.diff_completed_callback = diffcompleted_callback
        self.all_done_callback = alldone_callback
        self.cancel_callback = cancel_callback

    def my_print(self, msg):
        if self.print_callback:
            self.print_callback(msg)
            # print(msg) #can uncomment to debug
        else:  # pragma: no cover
            print(msg)

    def my_starting(self, number_of_builds, number_of_cases_per_build):
        if self.starting_callback:
            self.starting_callback(number_of_builds, number_of_cases_per_build)
        else:  # pragma: no cover
            self.my_print(
                "Starting runtests, # builds = %i, # cases per build = %i" % (
                    number_of_builds,
                    number_of_cases_per_build
                )
            )

    def my_casecompleted(self, test_case_completed_instance):
        if self.case_completed_callback:
            self.case_completed_callback(test_case_completed_instance)
        else:  # pragma: no cover
            self.my_print(
                "Case complete: %s : %s" % (
                    test_case_completed_instance.run_directory,
                    test_case_completed_instance.case_name
                )
            )

    def my_simulationscomplete(self):
        if self.simulations_complete_callback:
            self.simulations_complete_callback()
        else:  # pragma: no cover
            self.my_print("Completed all simulations")

    def my_diffcompleted(self, case_name):
        if self.diff_completed_callback:
            self.diff_completed_callback(case_name)
        else:  # pragma: no cover
            self.my_print("Completed diffing case: %s" % case_name)

    def my_alldone(self, results):
        if self.all_done_callback:
            self.all_done_callback(results)
        else:  # pragma: no cover
            self.my_print("Completed runtests")

    def my_cancelled(self):  # pragma: no cover
        if self.cancel_callback:
            self.cancel_callback()
        else:
            self.my_print("Cancelling runtests...")

    def interrupt_please(self):  # pragma: no cover
        self.id_like_to_stop_now = True


if __name__ == "__main__":  # pragma: no cover
    from epregressions.builds.makefile import CMakeCacheMakeFileBuildDirectory

    # parse command line arguments
    parser = argparse.ArgumentParser(
        description="""
    Run EnergyPlus tests using a specified configuration.  Can be executed in 2 ways:
      1: Arguments can be passed from the command line in the usage here, or
      2: An instance of the SuiteRunner class can be constructed, more useful for UIs or scripting"""
    )
    parser.add_argument('a_src', action="store", help='Path to case a\'s source repository root')
    parser.add_argument('a_build', action="store", help='Path to case a\'s build directory')
    parser.add_argument('b_src', action="store", help='Path to case b\'s source repository root')
    parser.add_argument('b_build', action="store", help='Path to case b\'s build directory')
    parser.add_argument('idf_list_file', action='store', help='Path to the file containing the list of IDFs to run')
    parser.add_argument('-a', action="store_true", help='Use this flag to run case a files')
    parser.add_argument('-b', action="store_true", help='Use this flag to run case b files')
    parser.add_argument('-f', choices=['DD', 'Annual'], help='Force a specific run type', default=None)
    parser.add_argument('-j', action="store", dest="j", type=int, default=1, help='Number of processors to use')
    parser.add_argument('-t', action='store_true', default=False, help='Use this flag to run in test mode')

    args = parser.parse_args()

    run_type = ForceRunType.NONE
    if args.f:
        if args.f == 'DD':
            run_type = ForceRunType.DD
        elif args.f == 'Annual':
            run_type = ForceRunType.ANNUAL

    # For ALL runs use BuildA
    base = CMakeCacheMakeFileBuildDirectory()
    base.run = True
    base.set_build_directory(args.a_build)

    # If using ReverseDD, builB can just be None
    mod = CMakeCacheMakeFileBuildDirectory()
    mod.run = True
    mod.set_build_directory(args.b_build)

    # Do a single test run...
    DoASingleTestRun = args.t

    # Set the expected path for the files_to_run.txt file
    if not os.path.exists(args.idf_list_file):
        print("ERROR: Did not find files_to_run.txt at %s; run build_files_to_run first!" % args.idf_list_file)
        sys.exit(1)

    # Build the list of files to run here:
    entries = []
    with io.open(args.idf_list_file, encoding='utf-8') as f:  # need to ask for this name separately
        json_object = json.loads(f.read())
        for entry in json_object['files_to_run']:
            basename = entry['file']
            if 'epw' in entry:
                epw = entry['epw']
            else:
                epw = None
            entries.append(TestEntry(basename, epw))
            if DoASingleTestRun:
                break

    # Build the run configuration
    RunConfig = TestRunConfiguration(force_run_type=run_type,
                                     single_test_run=DoASingleTestRun,
                                     num_threads=args.j,
                                     report_freq=ReportingFreq.HOURLY,
                                     build_a=base,
                                     build_b=mod)

    # instantiate the test suite
    Runner = SuiteRunner(RunConfig, entries)

    # Run it
    Runner.run_test_suite()
