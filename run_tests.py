#!/usr/bin/env python
# Copyright 2011-2013, Damian Johnson
# See LICENSE for licensing information

"""
Runs unit and integration tests. For usage information run this with '--help'.
"""

import getopt
import os
import shutil
import StringIO
import sys
import threading
import time
import unittest

import stem.prereq
import stem.util.conf
import stem.util.enum

from stem.util import log, system, term

import test.output
import test.runner
import test.static_checks

from test.runner import Target

OPT = "auist:l:h"
OPT_EXPANDED = ["all", "unit", "integ", "style", "python3", "clean", "targets=", "test=", "log=", "tor=", "help"]
DIVIDER = "=" * 70

CONFIG = stem.util.conf.config_dict("test", {
  "msg.help": "",
  "target.description": {},
  "target.prereq": {},
  "target.torrc": {},
  "integ.test_directory": "./test/data",
})

DEFAULT_RUN_TARGET = Target.RUN_OPEN

base = os.path.sep.join(__file__.split(os.path.sep)[:-1]).lstrip("./")
SOURCE_BASE_PATHS = [os.path.join(base, path) for path in ('stem', 'test', 'run_tests.py')]


def _clean_orphaned_pyc():
  test.output.print_noline("  checking for orphaned .pyc files... ", *test.runner.STATUS_ATTR)

  orphaned_pyc = []

  for base_dir in SOURCE_BASE_PATHS:
    for pyc_path in test.static_checks._get_files_with_suffix(base_dir, ".pyc"):
      # If we're running python 3 then the *.pyc files are no longer bundled
      # with the *.py. Rather, they're in a __pycache__ directory.
      #
      # At the moment there's no point in checking for orphaned bytecode with
      # python 3 because it's an exported copy of the python 2 codebase, so
      # skipping.

      if "__pycache__" in pyc_path:
        continue

      if not os.path.exists(pyc_path[:-1]):
        orphaned_pyc.append(pyc_path)

  if not orphaned_pyc:
    # no orphaned files, nothing to do
    test.output.print_line("done", *test.runner.STATUS_ATTR)
  else:
    print
    for pyc_file in orphaned_pyc:
      test.output.print_error("    removing %s" % pyc_file)
      os.remove(pyc_file)


def _python3_setup(python3_destination, clean):
  # Python 2.7.3 added some nice capabilities to 2to3, like '--output-dir'...
  #
  #   http://docs.python.org/2/library/2to3.html
  #
  # ... but I'm using 2.7.1, and it's pretty easy to make it work without
  # requiring a bleeding edge interpretor.

  test.output.print_divider("EXPORTING TO PYTHON 3", True)

  if clean:
    shutil.rmtree(python3_destination, ignore_errors = True)

  if os.path.exists(python3_destination):
    test.output.print_error("Reusing '%s'. Run again with '--clean' if you want to recreate the python3 export." % python3_destination)
    print
    return True

  os.makedirs(python3_destination)

  try:
    # skips the python3 destination (to avoid an infinite loop)
    def _ignore(src, names):
      if src == os.path.normpath(python3_destination):
        return names
      else:
        return []

    test.output.print_noline("  copying stem to '%s'... " % python3_destination, *test.runner.STATUS_ATTR)
    shutil.copytree('stem', os.path.join(python3_destination, 'stem'))
    shutil.copytree('test', os.path.join(python3_destination, 'test'), ignore = _ignore)
    shutil.copy('run_tests.py', os.path.join(python3_destination, 'run_tests.py'))
    test.output.print_line("done", *test.runner.STATUS_ATTR)
  except OSError, exc:
    test.output.print_error("failed\n%s" % exc)
    return False

  try:
    test.output.print_noline("  running 2to3... ", *test.runner.STATUS_ATTR)
    system.call("2to3 --write --nobackups --no-diffs %s" % python3_destination)
    test.output.print_line("done", *test.runner.STATUS_ATTR)
  except OSError, exc:
    test.output.print_error("failed\n%s" % exc)
    return False

  return True


def _print_style_issues(run_unit, run_integ, run_style):
  style_issues = test.static_checks.get_issues(SOURCE_BASE_PATHS)

  # If we're doing some sort of testing (unit or integ) and pyflakes is
  # available then use it. Its static checks are pretty quick so there's not
  # much overhead in including it with all tests.

  if run_unit or run_integ:
    if system.is_available("pyflakes"):
      style_issues.update(test.static_checks.pyflakes_issues(SOURCE_BASE_PATHS))
    else:
      test.output.print_error("Static error checking requires pyflakes. Please install it from ...\n  http://pypi.python.org/pypi/pyflakes\n")

  if run_style:
    if system.is_available("pep8"):
      style_issues.update(test.static_checks.pep8_issues(SOURCE_BASE_PATHS))
    else:
      test.output.print_error("Style checks require pep8. Please install it from...\n  http://pypi.python.org/pypi/pep8\n")

  if style_issues:
    test.output.print_line("STYLE ISSUES", term.Color.BLUE, term.Attr.BOLD)

    for file_path in style_issues:
      test.output.print_line("* %s" % file_path, term.Color.BLUE, term.Attr.BOLD)

      for line_number, msg in style_issues[file_path]:
        line_count = "%-4s" % line_number
        test.output.print_line("  line %s - %s" % (line_count, msg))

      print


if __name__ == '__main__':
  try:
    stem.prereq.check_requirements()
  except ImportError, exc:
    print exc
    print

    sys.exit(1)

  start_time = time.time()

  # override flag to indicate at the end that testing failed somewhere
  testing_failed = False

  # count how many tests have been skipped.
  skipped_test_count = 0

  # loads and validates our various configurations
  test_config = stem.util.conf.get_config("test")

  settings_path = os.path.join(test.runner.STEM_BASE, "test", "settings.cfg")
  test_config.load(settings_path)

  try:
    opts = getopt.getopt(sys.argv[1:], OPT, OPT_EXPANDED)[0]
  except getopt.GetoptError, exc:
    print "%s (for usage provide --help)" % exc
    sys.exit(1)

  run_unit = False
  run_integ = False
  run_style = False
  run_python3 = False
  run_python3_clean = False

  test_prefix = None
  logging_runlevel = None
  tor_path = "tor"

  # Integration testing targets fall into two categories:
  #
  # * Run Targets (like RUN_COOKIE and RUN_PTRACE) which customize our torrc.
  #   We do an integration test run for each run target we get.
  #
  # * Attribute Target (like CHROOT and ONLINE) which indicates
  #   non-configuration changes to ur test runs. These are applied to all
  #   integration runs that we perform.

  run_targets = [DEFAULT_RUN_TARGET]
  attribute_targets = []

  for opt, arg in opts:
    if opt in ("-a", "--all"):
      run_unit = True
      run_integ = True
      run_style = True
    elif opt in ("-u", "--unit"):
      run_unit = True
    elif opt in ("-i", "--integ"):
      run_integ = True
    elif opt in ("-s", "--style"):
      run_style = True
    elif opt == "--python3":
      run_python3 = True
    elif opt == "--clean":
      run_python3_clean = True
    elif opt in ("-t", "--targets"):
      integ_targets = arg.split(",")

      run_targets = []
      all_run_targets = [t for t in Target if CONFIG["target.torrc"].get(t) is not None]

      # validates the targets and split them into run and attribute targets

      if not integ_targets:
        print "No targets provided"
        sys.exit(1)

      for target in integ_targets:
        if not target in Target:
          print "Invalid integration target: %s" % target
          sys.exit(1)
        elif target in all_run_targets:
          run_targets.append(target)
        else:
          attribute_targets.append(target)

      # check if we were told to use all run targets

      if Target.RUN_ALL in attribute_targets:
        attribute_targets.remove(Target.RUN_ALL)
        run_targets = all_run_targets
    elif opt in ("-l", "--test"):
      test_prefix = arg
    elif opt in ("-l", "--log"):
      logging_runlevel = arg.upper()
    elif opt in ("--tor"):
      tor_path = arg
    elif opt in ("-h", "--help"):
      # Prints usage information and quits. This includes a listing of the
      # valid integration targets.

      print CONFIG["msg.help"]

      # gets the longest target length so we can show the entries in columns
      target_name_length = max(map(len, Target))
      description_format = "    %%-%is - %%s" % target_name_length

      for target in Target:
        print description_format % (target, CONFIG["target.description"].get(target, ""))

      print

      sys.exit()

  # basic validation on user input

  if logging_runlevel and not logging_runlevel in log.LOG_VALUES:
    print "'%s' isn't a logging runlevel, use one of the following instead:" % logging_runlevel
    print "  TRACE, DEBUG, INFO, NOTICE, WARN, ERROR"
    sys.exit(1)

  # check that we have 2to3 and python3 available in our PATH
  if run_python3:
    for required_cmd in ("2to3", "python3"):
      if not system.is_available(required_cmd):
        test.output.print_error("Unable to test python 3 because %s isn't in your path" % required_cmd)
        sys.exit(1)

  if run_python3 and sys.version_info[0] != 3:
    python3_destination = os.path.join(CONFIG["integ.test_directory"], "python3")

    if _python3_setup(python3_destination, run_python3_clean):
      python3_runner = os.path.join(python3_destination, "run_tests.py")
      exit_status = os.system("python3 %s %s" % (python3_runner, " ".join(sys.argv[1:])))
      sys.exit(exit_status)
    else:
      sys.exit(1)  # failed to do python3 setup

  if not run_unit and not run_integ and not run_style:
    test.output.print_line("Nothing to run (for usage provide --help)\n")
    sys.exit()

  # if we have verbose logging then provide the testing config
  our_level = stem.util.log.logging_level(logging_runlevel)
  info_level = stem.util.log.logging_level(stem.util.log.INFO)

  if our_level <= info_level:
    test.output.print_config(test_config)

  error_tracker = test.output.ErrorTracker()
  output_filters = (
    error_tracker.get_filter(),
    test.output.strip_module,
    test.output.align_results,
    test.output.colorize,
  )

  stem_logger = log.get_logger()
  logging_buffer = log.LogBuffer(logging_runlevel)
  stem_logger.addHandler(logging_buffer)

  test.output.print_divider("INITIALISING", True)

  test.output.print_line("Performing startup activities...", *test.runner.STATUS_ATTR)
  _clean_orphaned_pyc()

  print

  if run_unit:
    test.output.print_divider("UNIT TESTS", True)
    error_tracker.set_category("UNIT TEST")

    for test_class in test.runner.get_unit_tests(test_prefix):
      test.output.print_divider(test_class.__module__)
      suite = unittest.TestLoader().loadTestsFromTestCase(test_class)
      test_results = StringIO.StringIO()
      run_result = unittest.TextTestRunner(test_results, verbosity=2).run(suite)
      if stem.prereq.is_python_27():
        skipped_test_count += len(run_result.skipped)

      sys.stdout.write(test.output.apply_filters(test_results.getvalue(), *output_filters))
      print

      test.output.print_logging(logging_buffer)

    print

  if run_integ:
    test.output.print_divider("INTEGRATION TESTS", True)
    integ_runner = test.runner.get_runner()

    # Determine targets we don't meet the prereqs for. Warnings are given about
    # these at the end of the test run so they're more noticeable.

    our_version = stem.version.get_system_tor_version(tor_path)
    skip_targets = []

    for target in run_targets:
      # check if we meet this target's tor version prerequisites

      target_prereq = CONFIG["target.prereq"].get(target)

      if target_prereq and our_version < stem.version.Requirement[target_prereq]:
        skip_targets.append(target)
        continue

      error_tracker.set_category(target)

      try:
        # converts the 'target.torrc' csv into a list of test.runner.Torrc enums
        config_csv = CONFIG["target.torrc"].get(target)
        torrc_opts = []

        if config_csv:
          for opt in config_csv.split(','):
            opt = opt.strip()

            if opt in test.runner.Torrc.keys():
              torrc_opts.append(test.runner.Torrc[opt])
            else:
              test.output.print_line("'%s' isn't a test.runner.Torrc enumeration" % opt)
              sys.exit(1)

        integ_runner.start(target, attribute_targets, tor_path, extra_torrc_opts = torrc_opts)

        test.output.print_line("Running tests...", term.Color.BLUE, term.Attr.BOLD)
        print

        for test_class in test.runner.get_integ_tests(test_prefix):
          test.output.print_divider(test_class.__module__)
          suite = unittest.TestLoader().loadTestsFromTestCase(test_class)
          test_results = StringIO.StringIO()
          run_result = unittest.TextTestRunner(test_results, verbosity=2).run(suite)
          if stem.prereq.is_python_27():
            skipped_test_count += len(run_result.skipped)

          sys.stdout.write(test.output.apply_filters(test_results.getvalue(), *output_filters))
          print

          test.output.print_logging(logging_buffer)

        # We should have joined on all threads. If not then that indicates a
        # leak that could both likely be a bug and disrupt further targets.

        active_threads = threading.enumerate()

        if len(active_threads) > 1:
          test.output.print_error("Threads lingering after test run:")

          for lingering_thread in active_threads:
            test.output.print_error("  %s" % lingering_thread)

          testing_failed = True
          break
      except KeyboardInterrupt:
        test.output.print_error("  aborted starting tor: keyboard interrupt\n")
        break
      except OSError:
        testing_failed = True
      finally:
        integ_runner.stop()

    if skip_targets:
      print

      for target in skip_targets:
        req_version = stem.version.Requirement[CONFIG["target.prereq"][target]]
        test.output.print_line("Unable to run target %s, this requires tor version %s" % (target, req_version), term.Color.RED, term.Attr.BOLD)

      print

    # TODO: note unused config options afterward?

  if not stem.prereq.is_python_3():
    _print_style_issues(run_unit, run_integ, run_style)

  runtime = time.time() - start_time

  if runtime < 1:
    runtime_label = "(%0.1f seconds)" % runtime
  else:
    runtime_label = "(%i seconds)" % runtime

  has_error = testing_failed or error_tracker.has_error_occured()

  if has_error:
    test.output.print_error("TESTING FAILED %s" % runtime_label)

    for line in error_tracker:
      test.output.print_error("  %s" % line)
  elif skipped_test_count > 0:
    test.output.print_line("%i TESTS WERE SKIPPED" % skipped_test_count, term.Color.BLUE, term.Attr.BOLD)
    test.output.print_line("ALL OTHER TESTS PASSED %s" % runtime_label, term.Color.GREEN, term.Attr.BOLD)
    print
  else:
    test.output.print_line("TESTING PASSED %s" % runtime_label, term.Color.GREEN, term.Attr.BOLD)
    print

  sys.exit(1 if has_error else 0)
