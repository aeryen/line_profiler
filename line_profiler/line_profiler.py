#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

try:
    import cPickle as pickle
except ImportError:
    import pickle

try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO
import functools
import inspect
import linecache
import tempfile
import os
import sys
from argparse import ArgumentError, ArgumentParser

from IPython.core.magic import (Magics, magics_class, line_magic)
from IPython.core.page import page
from IPython.utils.ipstruct import Struct
from IPython.core.error import UsageError

try:
    from ._line_profiler import LineProfiler as CLineProfiler
except ImportError as ex:
    raise ImportError(
        'The line_profiler._line_profiler c-extension is not importable. '
        'Has it been compiled? Underlying error is ex={!r}'.format(ex)
    )

__version__ = '3.3.1'

# Python 2/3 compatibility utils
# ===========================================================
PY3 = sys.version_info[0] == 3
PY35 = PY3 and sys.version_info[1] >= 5

# exec (from https://bitbucket.org/gutworth/six/):
if PY3:
    import builtins
    exec_ = getattr(builtins, "exec")
    del builtins
else:
    def exec_(_code_, _globs_=None, _locs_=None):
        """Execute code in a namespace."""
        if _globs_ is None:
            frame = sys._getframe(1)
            _globs_ = frame.f_globals
            if _locs_ is None:
                _locs_ = frame.f_locals
            del frame
        elif _locs_ is None:
            _locs_ = _globs_
        exec("""exec _code_ in _globs_, _locs_""")

if PY35:
    import inspect
    def is_coroutine(f):
        return inspect.iscoroutinefunction(f)
else:
    def is_coroutine(f):
        return False

# ============================================================

CO_GENERATOR = 0x0020
def is_generator(f):
    """ Return True if a function is a generator.
    """
    isgen = (f.__code__.co_flags & CO_GENERATOR) != 0
    return isgen


class LineProfiler(CLineProfiler):
    """ A profiler that records the execution times of individual lines.
    """

    def __call__(self, func):
        """ Decorate a function to start the profiler on function entry and stop
        it on function exit.
        """
        self.add_function(func)
        if is_coroutine(func):
            wrapper = self.wrap_coroutine(func)
        elif is_generator(func):
            wrapper = self.wrap_generator(func)
        else:
            wrapper = self.wrap_function(func)
        return wrapper

    def wrap_generator(self, func):
        """ Wrap a generator to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            g = func(*args, **kwds)
            # The first iterate will not be a .send()
            self.enable_by_count()
            try:
                item = next(g)
            except StopIteration:
                return
            finally:
                self.disable_by_count()
            input = (yield item)
            # But any following one might be.
            while True:
                self.enable_by_count()
                try:
                    item = g.send(input)
                except StopIteration:
                    return
                finally:
                    self.disable_by_count()
                input = (yield item)
        return wrapper

    def wrap_function(self, func):
        """ Wrap a function to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result
        return wrapper

    if PY35:
        from . import line_profiler_py35
        wrap_coroutine = line_profiler_py35.wrap_coroutine

    def dump_stats(self, filename):
        """ Dump a representation of the data to a file as a pickled LineStats
        object from `get_stats()`.
        """
        lstats = self.get_stats()
        with open(filename, 'wb') as f:
            pickle.dump(lstats, f, pickle.HIGHEST_PROTOCOL)

    def print_stats(self, stream=None, output_unit=None, stripzeros=False):
        """ Show the gathered statistics.
        """
        lstats = self.get_stats()
        show_text(lstats.timings, lstats.unit, output_unit=output_unit, stream=stream, stripzeros=stripzeros)

    def run(self, cmd):
        """ Profile a single executable statment in the main namespace.
        """
        import __main__
        main_dict = __main__.__dict__
        return self.runctx(cmd, main_dict, main_dict)

    def runctx(self, cmd, globals, locals):
        """ Profile a single executable statement in the given namespaces.
        """
        self.enable_by_count()
        try:
            exec_(cmd, globals, locals)
        finally:
            self.disable_by_count()
        return self

    def runcall(self, func, *args, **kw):
        """ Profile a single function call.
        """
        self.enable_by_count()
        try:
            return func(*args, **kw)
        finally:
            self.disable_by_count()

    def add_module(self, mod):
        """ Add all the functions in a module and its classes.
        """
        from inspect import isclass, isfunction

        nfuncsadded = 0
        for item in mod.__dict__.values():
            if isclass(item):
                for k, v in item.__dict__.items():
                    if isfunction(v):
                        self.add_function(v)
                        nfuncsadded += 1
            elif isfunction(item):
                self.add_function(item)
                nfuncsadded += 1

        return nfuncsadded


def is_ipython_kernel_cell(filename):
    """ Return True if a filename corresponds to a Jupyter Notebook cell
    """
    return (
        filename.startswith("<ipython-input-") or
        filename.startswith(tempfile.gettempdir() + '/ipykernel_') or
        filename.startswith(tempfile.gettempdir() + '/xpython_')
    )


def show_func(filename, start_lineno, func_name, timings, unit,
    output_unit=None, stream=None, stripzeros=False):
    """ Show results for a single function.
    """
    if stream is None:
        stream = sys.stdout

    template = '%6s %9s %12s %8s %8s  %-s'
    d = {}
    total_time = 0.0
    linenos = []
    for lineno, nhits, time in timings:
        total_time += time
        linenos.append(lineno)

    if stripzeros and total_time == 0:
        return

    if output_unit is None:
        output_unit = unit
    scalar = unit / output_unit

    stream.write("Total time: %g s\n" % (total_time * unit))
    if os.path.exists(filename) or is_ipython_kernel_cell(filename):
        stream.write("File: %s\n" % filename)
        stream.write("Function: %s at line %s\n" % (func_name, start_lineno))
        if os.path.exists(filename):
            # Clear the cache to ensure that we get up-to-date results.
            linecache.clearcache()
        all_lines = linecache.getlines(filename)
        sublines = inspect.getblock(all_lines[start_lineno-1:])
    else:
        stream.write("\n")
        stream.write("Could not find file %s\n" % filename)
        stream.write("Are you sure you are running this program from the same directory\n")
        stream.write("that you ran the profiler from?\n")
        stream.write("Continuing without the function's contents.\n")
        # Fake empty lines so we can see the timings, if not the code.
        nlines = max(linenos) - min(min(linenos), start_lineno) + 1
        sublines = [''] * nlines
    for lineno, nhits, time in timings:
        d[lineno] = (nhits,
            '%5.1f' % (time * scalar),
            '%5.1f' % (float(time) * scalar / nhits),
            '%5.1f' % (100 * time / total_time) )
    linenos = range(start_lineno, start_lineno + len(sublines))
    empty = ('', '', '', '')
    header = template % ('Line #', 'Hits', 'Time', 'Per Hit', '% Time',
        'Line Contents')
    stream.write("\n")
    stream.write(header)
    stream.write("\n")
    stream.write('=' * len(header))
    stream.write("\n")
    for lineno, line in zip(linenos, sublines):
        nhits, time, per_hit, percent = d.get(lineno, empty)
        txt = template % (lineno, nhits, time, per_hit, percent,
                          line.rstrip('\n').rstrip('\r'))
        stream.write(txt)
        stream.write("\n")
    stream.write("\n")

def show_text(stats, unit, output_unit=None, stream=None, stripzeros=False):
    """ Show text for the given timings.
    """
    if stream is None:
        stream = sys.stdout

    if output_unit is not None:
        stream.write('Timer unit: %g s\n\n' % output_unit)
    else:
        stream.write('Timer unit: %g s\n\n' % unit)

    for (fn, lineno, name), timings in sorted(stats.items()):
        show_func(fn, lineno, name, stats[fn, lineno, name], unit,
            output_unit=output_unit, stream=stream, stripzeros=stripzeros)

@magics_class
class LineProfilerMagics(Magics):

    @line_magic
    def lprun(self, parameter_s=''):
        """ Execute a statement under the line-by-line profiler from the
        line_profiler module.

        Usage:
          %lprun -f func1 -f func2 <statement>

        The given statement (which doesn't require quote marks) is run via the
        LineProfiler. Profiling is enabled for the functions specified by the -f
        options. The statistics will be shown side-by-side with the code through the
        pager once the statement has completed.

        Options:

        -f <function>: LineProfiler only profiles functions and methods it is told
        to profile.  This option tells the profiler about these functions. Multiple
        -f options may be used. The argument may be any expression that gives
        a Python function or method object. However, one must be careful to avoid
        spaces that may confuse the option parser.

        -m <module>: Get all the functions/methods in a module

        One or more -f or -m options are required to get any useful results.

        -D <filename>: dump the raw statistics out to a pickle file on disk. The
        usual extension for this is ".lprof". These statistics may be viewed later
        by running line_profiler.py as a script.

        -T <filename>: dump the text-formatted statistics with the code side-by-side
        out to a text file.

        -r: return the LineProfiler object after it has completed profiling.

        -s: strip out all entries from the print-out that have zeros.

        -u: specify time unit for the print-out in seconds.
        """

        # Escape quote markers.
        opts_def = Struct(D=[''], T=[''], f=[], m=[], u=None)
        parameter_s = parameter_s.replace('"', r'\"').replace("'", r"\'")
        opts, arg_str = self.parse_options(parameter_s, 'rsf:m:D:T:u:', list_all=True)
        opts.merge(opts_def)

        global_ns = self.shell.user_global_ns
        local_ns = self.shell.user_ns

        # Get the requested functions.
        funcs = []
        for name in opts.f:
            try:
                funcs.append(eval(name, global_ns, local_ns))
            except Exception as e:
                raise UsageError('Could not find function %r.\n%s: %s' % (name,
                    e.__class__.__name__, e))

        profile = LineProfiler(*funcs)

        # Get the modules, too
        for modname in opts.m:
            try:
                mod = __import__(modname, fromlist=[''])
                profile.add_module(mod)
            except Exception as e:
                raise UsageError('Could not find module %r.\n%s: %s' % (modname,
                    e.__class__.__name__, e))

        if opts.u is not None:
            try:
                output_unit = float(opts.u[0])
            except Exception as e:
                raise TypeError("Timer unit setting must be a float.")
        else:
            output_unit = None

        # Add the profiler to the builtins for @profile.
        if PY3:
            import builtins
        else:
            import __builtin__ as builtins

        if 'profile' in builtins.__dict__:
            had_profile = True
            old_profile = builtins.__dict__['profile']
        else:
            had_profile = False
            old_profile = None
        builtins.__dict__['profile'] = profile

        try:
            try:
                profile.runctx(arg_str, global_ns, local_ns)
                message = ''
            except SystemExit:
                message = """*** SystemExit exception caught in code being profiled."""
            except KeyboardInterrupt:
                message = ("*** KeyboardInterrupt exception caught in code being "
                    "profiled.")
        finally:
            if had_profile:
                builtins.__dict__['profile'] = old_profile

        # Trap text output.
        stdout_trap = StringIO()
        profile.print_stats(stdout_trap, output_unit=output_unit, stripzeros='s' in opts)
        output = stdout_trap.getvalue()
        output = output.rstrip()

        page(output)
        print(message, end="")

        dump_file = opts.D[0]
        if dump_file:
            profile.dump_stats(dump_file)
            print('\n*** Profile stats pickled to file %r. %s' % (
                dump_file, message))

        text_file = opts.T[0]
        if text_file:
            pfile = open(text_file, 'w')
            pfile.write(output)
            pfile.close()
            print('\n*** Profile printout saved to text file %r. %s' % (
                text_file, message))

        return_value = None
        if 'r' in opts:
            return_value = profile

        return return_value


def load_ipython_extension(ip):
    """ API for IPython to recognize this module as an IPython extension.
    """
    ip.register_magics(LineProfilerMagics)


def load_stats(filename):
    """ Utility function to load a pickled LineStats object from a given
    filename.
    """
    with open(filename, 'rb') as f:
        return pickle.load(f)


def main():
    def positive_float(value):
        val = float(value)
        if val <= 0:
            raise ArgumentError
        return val

    parser = ArgumentParser()
    parser.add_argument('-V', '--version', action='version', version=__version__)
    parser.add_argument(
        '-u',
        '--unit',
        default='1e-6',
        type=positive_float,
        help="Output unit (in seconds) in which the timing info is displayed (default: 1e-6)",
    )
    parser.add_argument(
        '-z',
        '--skip-zero',
        action='store_true',
        help="Hide functions which have not been called",
    )
    parser.add_argument('profile_output', help="*.lprof file created by kernprof")

    args = parser.parse_args()
    lstats = load_stats(args.profile_output)
    show_text(lstats.timings, lstats.unit, output_unit=args.unit, stripzeros=args.skip_zero)


if __name__ == '__main__':
    main()
