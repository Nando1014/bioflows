# BioFlow - Package for  automating bio-informatics workflows
# Copyright (c) 2012-2014 Brown University. All rights reserved.
#
# This file is part of BioFlows.
#
# BioFlows is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# BioFlows is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with BioFlows.  If not, see <http://www.gnu.org/licenses/>.

"""
A series of wrappers for external calls to various bioinformatics tools.
"""

import copy
import hashlib
import os
import subprocess
import sys
from itertools import chain

# import config
# import diagnostics
import utils


class BaseWrapper(object):
    """
    A base class that handles generic wrapper functionality.

    Wrappers for specific programs should inherit this class, call `self.init`
    to specify their `name` (which is a key into the executable entries in the
    BioLite configuration file), and append their arguments to the `self.args`
    list.

    By convention, a wrapper should call `self.run()` as the final line in its
    `__init__` function. This allows for clean syntax and use of the wrapper
    directly, without assigning it to a variable name, e.g.

    wrappers.MyWrapper(arg1, arg2, ...)

    When your wrapper runs, BaseWrapper will do the following:

    * log the complete command line to diagnostics;
    * optionally call the program with a version flag (invoked with `version`)
      to obtain a version string, then log this to the :ref:`programs-table`
      along with a hash of the binary executable file;
    * append the command's stderr to a file called `name`.log in the CWD;
    * also append the command's stdout to the same log file, unless you set
      `self.stdout`, in which case stdout is redirected to a file of that name;
    * on Linux, add a memory profiling library to the LD_PRELOAD environment
      variable;
    * call the command and check its return code (which should be 0 on success,
      unless you specify a different code with `self.return_ok`), optionally
      using the CWD specified in `self.cwd` or the environment specified in
      `self.env`.
    * parse the stderr of the command to find [biolite.profile] markers and
      use the rusage values from `utils.safe_call` to populate a profile
      entity in the diagnostics with walltime, usertime, systime, mem, and
      vmem attributes.
    """
    # hold the input/output suffixes for output
    in_suffix = ''
    out_suffix = ''
    add_args = []
    target = ''
    stdout = ''
    args = []
    input = ''

    def __init__(self, name, **kwargs):

        self.name = name
        self.prog_id = kwargs.get('prog_id')
        # self.shell = '/bin/sh'
        self.cmd = None
        self.run_command = None
        self.args = []
        self.conda_command = kwargs.get('conda_command')
        self.job_parms = kwargs.get('job_parms')
        self.paired_end = kwargs.get('paired_end', False)
        self.cwd = kwargs.get('cwd', os.getcwd())
        self.log_dir = kwargs.get('log_dir', os.path.join(os.getcwd(), "logs"))
        self.align_dir = kwargs.get('align_dir', os.path.join(os.getcwd(), 'align_dir'))
        self.qc_dir = kwargs.get('qc_dir', os.path.join(os.getcwd(), 'qc_dir'))
        self.scripts_dir = kwargs.get('scripts_dir', os.path.join(self.log_dir, 'scripts_dir'))
        self.intermediary_dir = kwargs.get('intermediary_dir', os.path.join(os.getcwd(), 'intermediary_files'))
        ## Define the checkpoint files
        ##self.luigi_source = os.path.join(self.cwd, 'checkpoints', kwargs.get('source', "None"))
        self.luigi_source = "Present"
        self.luigi_target = os.path.join(self.cwd, 'checkpoints', kwargs.get('target', "None"))

        ## Below for testing only
        self.local_target = kwargs.get('local_targets', True)
        if self.local_target:
            self.luigi_local_target = os.path.join(
                kwargs.get('luigi_local_path', "/Users/aragaven/scratch/test_workflow"),
                kwargs.get('target', "None"))
        self.stdout = kwargs.get('stdout')
        self.stderr = kwargs.get('stderr')
        self.stdout_append = kwargs.get('stdout_append')
        # self.pipe = kwargs.get('pipe')
        self.env = os.environ.copy()
        self.max_concurrency = kwargs.get('max_concurrency', 1)
        self.prog_args = dict()
        for k, v in kwargs.iteritems():
            self.prog_args[k] = v
        self.setup_command()
        # self.output_patterns = None
        return

    init = __init__
    """A shortcut for calling the BaseWrapper __init__ from a subclass."""

    # def check_input(self, flag, path):
    #     """
    #     Turns path into an absolute path and checks that it exists, then
    #     appends it to the args using the given flag (or None).
    #     """
    #     path = os.path.abspath(path)
    #     if os.path.exists(path):
    #         if flag:
    #             self.args.append(flag)
    #         self.args.append(path)
    #     else:
    #         utils.die("input file for flag '%s' does not exists:\n  %s" % (flag, path))

    def prog_name_clean(self, name):
        """
        A function to strip the run name from programs
        :return:
        """
        if 'round' in name:
            input_list = name.split('_')
            idx_to_rm = [i for i, s in enumerate(input_list) if 'round' in s][0]
            del input_list[idx_to_rm:]
            new_name = '_'.join(input_list)
        else:
            new_name = name
        return new_name

    def add_threading(self, flag):
        """
        Indicates that this wrapper should use threading by appending an
        argument with the specified `flag` followed by the number of threads
        specified in the BioLite configuration file.
        """
        # threads = min(int(config.get_resource('threads')), self.max_concurrency)
        threads = self.max_concurrency
        if threads > 1:
            self.args.append(flag)
            self.args.append(threads)
        return

    def add_openmp(self):
        """
        Indicates that this wrapper should use OpenMP by setting the
        $OMP_NUM_THREADS environment variable equal to the number of threads
        specified in the BioLite configuration file.
        """
        ##threads = min(int(config.get_resource('threads')), self.max_concurrency)
        threads = self.max_concurrency
        self.env['OMP_NUM_THREADS'] = str(threads)

    def join_split_cmd(self, name):
        ''' Check if cmd needs to be split

         When cmds are passed in the YAML control file sub cmds are joined by an underscore
         This function will split the commands as needed to run the program
        :param name:
        :return:
        '''
        if len(name.split('_')) > 0:
            return ' '.join(name.split('_'))
        else:
            return name

    def setup_command(self):
        """
        Generate the command to be run based on the input. There are three cases and more can defined based on the YAML
        1) Just a name is given and this is also the executable name and is available in the PATH
        2) The name is given along-with a separate sub-command to be run. A simple example will be `samtools sort`
         this definition need to be implemented
        :param self: 
        :return: 
        """
        # Setup the command to run

        if not self.cmd:
            self.cmd = [self.name]  # change cmd to be name if specific command is not specified
        else:
            self.cmd = list(self.cmd.split())
        return

    # TODO fix this function
    def version(self, flag=None):
        """
        Generates and logs a hash to distinguish this particular installation
        of the program (on a certain host, with a certain compiler, program
        version, etc.)

        Specify the optional 'binary' argument if the wrapper name is not
        actually the program, e.g. if your program has a Perl wrapper script.
        Set 'binary' to the binary program that is likely to change between
        versions.

        Specify the optional 'cmd' argument if the command to run for version
        information is different than what will be invoked by `run` (e.g.
        if the program has a perl wrapper script, but you want to version an
        underlying binary executable).
        """
        cmd = copy.deepcopy(self.cmd)
        if flag:
            cmd.append(flag)
        else:
            cmd.append('-v')
        # Run the command.

        # self.env['PATH'] = self.conda_command.split()[2] + "/bin:" + self.env['PATH']
        print "\n ***** print PATH ***** \n"
        print self.env['PATH']
        try:
            vstring = subprocess.check_output(cmd, env=self.env, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            vstring = e.output
        except OSError as e:
            utils.failed_executable(cmd[0], e)

            # Generate a hash.
            # vhash = diagnostics.log_program_version(self.name, vstring, path)
            # if vhash:
            # 	diagnostics.prefix.append(self.name)
            # 	diagnostics.log('version', vhash)
            # 	diagnostics.prefix.pop()

    def version_jar(self):
        """
        Special case of version() when the executable is a JAR file.
        """
        # cmd = config.get_command('java')
        cmd = ['java ']
        cmd.append('-jar')
        cmd += self.cmd
        self.version(cmd=cmd, path=self.cmd[0])
        return

    def name_clean(self):
        """
        A function to strip the Xmx tags for Java programs
        and any other tags from other programs mainly the -T from gatk
        """
        input_list = self.name.split()
        if "Xmx" in self.name:
            idx_to_rm = [i for i, s in enumerate(input_list) if 'Xmx' in s][0]
            del input_list[idx_to_rm]
        if "-T" in self.name:
            idx_to_rm = [i for i, s in enumerate(input_list) if '-T' in s][0]
            del input_list[idx_to_rm]
        if "bqsr" in self.name:
            idx_to_rm = [i for i, s in enumerate(input_list) if 'bqsr' in s][0]
            del input_list[idx_to_rm]
        new_name = '_'.join(input_list)
        return new_name

    def setup_run(self, add_command=None):

        """
        Call this function at the end of your class's `__init__` function.
        """

        cmd = self.cmd
        stderr = os.path.join(self.log_dir, '_'.join([self.input, self.prog_id, 'err.log']))

        if self.stderr is not None:
            stderr = self.stderr
        if len(self.name.split()) > 1:
            stderr = os.path.join(self.log_dir, '_'.join([self.input, self.prog_id, 'err.log']))
            # MAYBE redundant
        self.args.append('2>>' + stderr)

        # if self.pipe:
        # 	self.args += ('|', self.pipe, '2>>' + stderr)

        # Write to a stdout file if it was set by the derived class.
        # Otherwise, stdout and stderr will be combined into the log file.

        if self.stdout:
            stdout = os.path.abspath(self.stdout)
            self.args.append('1>' + stdout)
        elif self.stdout_append:
            stdout = os.path.abspath(self.stdout_append)
            self.args.append('1>>' + stdout)
        else:
            self.args.append('1>>' + stderr)

        cmd = ' '.join(chain(cmd, map(str, self.args)))

        if add_command is not None:
            cmd += "; " + add_command

        self.run_command = cmd
        return

    def run_jar(self, mem=None):
        '''
        Special case of run() when the executable is a JAR file. This may be deprecated as we  will use conda for all
        packages

        :param mem:
        :return:
        '''

        # cmd = config.get_command('java')
        cmd = ['java ']
        if mem:
            cmd.append('-Xmx%s' % mem)
        cmd.append('-jar')
        cmd += self.cmd
        self.run(cmd)

    def update_file_suffix(self, input_default=None, output_default=None, **kwargs):
        """
        Update the file suffix for each wrapper based on whether custom input/output suffixes are provided. When the
        defaults for inputs and/or outputs are not provided then it is expected that the user will have to provide a
        suffix. For example with samtools view an output suffix is always required

        :param input_default:  Default suffix for input to the program
        :param output_default:  default suffix for output to the program
        :param kwargs:  this is new_base_kwargs passed on from the workflow class to each program
        :return:

        """

        if kwargs['suffix_type'] != "custom":
            if input_default is not None and output_default is not None:
                self.in_suffix = input_default
                self.out_suffix = output_default
            elif input_default is not None and output_default is None:
                print "Error!!! you need to specify an output suffix"
                sys.exit(0)
            elif input_default is None and output_default is not None:
                print "Error!!! you need to specify an output suffix"
                sys.exit(0)
            else:
                print "Error!!! you need to specify BOTH input  and output suffixes"
                sys.exit(0)
        else:
            if kwargs['suffix']['output'] != "default":
                self.out_suffix = kwargs['suffix']['output']
            else:
                self.out_suffix = output_default
            if kwargs['suffix']['input'] != "default":
                self.in_suffix = kwargs['suffix']['input']
            else:
                self.in_suffix = input_default
        return

    def reset_add_args(self):
        """
        This function needed to reset the add args when wrapper class is called multiple time
        Should perhaps be moved to the basewrapper class
        :return:

        """
        self.add_args = []
        return

### Third-party command line tools ###

class FastQC(BaseWrapper):
    """
    A wrapper for FastQC.
    http://www.bioinformatics.bbsrc.ac.uk/projects/fastqc/
    """


    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        kwargs['target'] = input + '.fastqc.zip.' + "_" + hashlib.sha224(input + '.fastqc.zip').hexdigest() + ".txt"

        # only need second part as fastqc is run on each file sequentially in the same job
        if kwargs.get('paired_end'):
            kwargs['target'] = input + '.2.fastqc' + "_" + hashlib.sha224(input + '.2.fastqc.zip').hexdigest() + ".txt"

        # Ssetup inputs/outputs
        self.update_file_suffix(input_default=".fq.gz", output_default="", **kwargs)

        kwargs['stdout_append'] = os.path.join(kwargs['log_dir'], input + '_' + name + '.log')
        kwargs['prog_id'] = name
        name = self.prog_name_clean(name)
        self.init(name, **kwargs)
        self.args += [' -o ' + self.qc_dir]
        self.args += args

        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))
        else:
            self.job_parms.update({'mem': 1000, 'time': 80, 'ncpus': 1})

        if self.paired_end:

            self.args.append(os.path.join(self.cwd, 'fastq', input + "_1" + self.in_suffix))
            self.setup_run()
            run_cmd1 = self.run_command

            ## Re initialize the object for the second in pair
            self.init(name, **kwargs)
            self.args += [' -o ' + self.qc_dir]
            self.args += args
            self.args.append(os.path.join(self.cwd, 'fastq', input + "_2" + self.in_suffix))
            self.setup_run()
            run_cmd2 = self.run_command

            self.run_command = run_cmd1 + "; " + run_cmd2
        else:
            self.args.append(os.path.join(self.cwd, 'fastq', input + self.in_suffix))
            self.setup_run()

        return


class Gsnap(BaseWrapper):
    """
    A wrapper for gsnap 
    
    """


    def __init__(self, name, input, *args, **kwargs):
        self.input = input

        ## Setup the inputs/output
        self.update_file_suffix(input_default=".fq.gz", output_default=".sam", **kwargs)
        ## set the checkpoint target file
        kwargs['target'] = input + self.out_suffix + "_" + hashlib.sha224(input + self.out_suffix).hexdigest() + ".txt"

        kwargs['stdout'] = os.path.join(kwargs['align_dir'], input + self.out_suffix)
        kwargs['prog_id'] = name

        name = self.prog_name_clean(name)
        self.init(name, **kwargs)

        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))
            if 'ncpus' in kwargs.get('add_job_parms').keys():
                # TODO make sure threads are not given in the args
                self.args += [' -t ' + str(kwargs.get('add_job_parms')['ncpus'])]
        else:
            self.job_parms.update({'mem': 1000, 'time': 80, 'ncpus': 1})

        if self.paired_end:
            kwargs['source'] = hashlib.sha224(input + '_2_fastqc.gzip').hexdigest() + ".txt"
        else:
            kwargs['source'] = hashlib.sha224(input + '_fastqc.gzip').hexdigest() + ".txt"

        self.setup_args(*args)

        self.args += args

        if self.paired_end:
            self.args.append(os.path.join(self.cwd, 'fastq', input + "_1" + self.in_suffix))
            self.args.append(os.path.join(self.cwd, 'fastq', input + "_2" + self.in_suffix))
        else:
            self.args.append(os.path.join(self.cwd, 'fastq', input + self.in_suffix))
        # self.cmd = ' '.join(chain(self.cmd, map(str, self.args), map(str,input)))

        self.setup_run()
        return

    def setup_args(self, *args):
        """
        setup default args
        :param args: The arguments from the Options section in the YAML
        :return:
        """
        if any("--gunzip" in a for a in args):
            pass
        else:
            self.args += ["--gunzip"]
        if any("-A sam" in a for a in args):
            pass
        else:
            self.args += ["-A sam"]
        if any("-N1" in a for a in args):
            pass
        else:
            self.args += ["-N1"]
        if any("--use-shared-memory=0" in a for a in args):
            pass
        else:
            self.args += ["--use-shared-memory=0"]
        return


class SamTools(BaseWrapper):
    '''
    Wrapper class for the samtools command
    '''
    stdout_as_output = False

    def __init__(self, name, input, *args, **kwargs):
        self.reset_add_args()
        self.input = input
        self.make_target(name, input, *args, **kwargs)
        kwargs['target'] = self.target
        if self.stdout_as_output:
            kwargs['stdout'] = os.path.join(kwargs['align_dir'], input + self.out_suffix)
        else:
            kwargs['stdout'] = os.path.join(kwargs['log_dir'], input + "_" + name + '.log')
        kwargs['prog_id'] = name
        name = self.prog_name_clean(name)

        new_name = ' '.join(name.split("_"))
        self.init(new_name, **kwargs)

        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))

            # add threading
            if name.split('_')[1] == "sort":
                if 'ncpus' in kwargs.get('add_job_parms').keys():
                    # TODO make sure threads are not given in the args
                    self.args += [' -@ ' + str(kwargs.get('add_job_parms')['ncpus'])]
                    if 'mem' in kwargs.get('add_job_parms').keys() and name.split('_')[1] == "sort":
                        # TODO make sure threads are not given in the args
                        mem_per_thread = int(kwargs.get('add_job_parms')['mem'] / kwargs.get('add_job_parms')['ncpus'])
                        self.args += [' -m ' + str(mem_per_thread) + "M"]
                    else:
                        mem_per_thread = int(kwargs.get('job_parms')['mem'] / kwargs.get('add_job_parms')['ncpus'])
                        self.args += [' -m ' + str(mem_per_thread) + "M"]
        else:
            self.job_parms.update({'mem': 4000, 'time': 300, 'ncpus': 1})
        # print self.add_args
        self.args += self.add_args
        self.setup_run()
        return


    def make_target(self, name, input, *args, **kwargs):
        # Make sure output suffix is enforced
        if kwargs['suffix_type'] != "custom":
            print "Error1!!! you need to specify an output suffix"
            sys.exit(0)
        else:
            self.out_suffix = kwargs['suffix']['output']
            if kwargs['suffix']['input'] != "default":
                self.in_suffix = kwargs['suffix']['input']
            else:
                self.in_suffix = "default"

        if name.split('_')[1] == "view":
            # self.stdout_as_output = True
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_view(input, *args, **kwargs)

        elif name.split('_')[1] == "sort":
            # self.stdout_as_output = True
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_sort(input, *args, **kwargs)

        elif name.split('_')[1] == "index":
            self.target = input + self.in_suffix + ".bai." + "_" + hashlib.sha224(
                input + self.in_suffix + ".bai").hexdigest() + ".txt"
            self.add_args_index(input, *args, **kwargs)
        return

    def add_args_view(self, input, *args, **kwargs):

        if self.in_suffix == "default":
            print "Error: You need to provide the input suffix\n"
            sys.exit(0)
        self.add_args += args
        self.add_args += ["-o", os.path.join(kwargs['align_dir'], input + self.out_suffix)]
        self.add_args.append(os.path.join(kwargs['align_dir'], input + self.in_suffix))
        return

    def add_args_sort(self, input, *args, **kwargs):
        if self.in_suffix == "default":
            self.in_suffix = ".bam"

        self.add_args += args
        if any("-T" in a for a in self.add_args):
            idx_to_replace = [i for i, s in enumerate(self.add_args) if '-T' in s][0]
            tmpdir_string = self.add_args[idx_to_replace] + "_" + input
            self.add_args[idx_to_replace] = tmpdir_string

        self.add_args += ["-o", os.path.join(kwargs['align_dir'], input + self.out_suffix)]
        self.add_args.append(os.path.join(kwargs['align_dir'], input + self.in_suffix))
        return

    def add_args_index(self, input, *args, **kwargs):
        if self.in_suffix == "default":
            self.in_suffix = ".bam"
        if any("-b" or "-c" or "-m" not in a for a in args):
            self.add_args += ["-b"]
            self.add_args += args
        else:
            self.add_args += args

        self.add_args.append(os.path.join(kwargs['align_dir'], input + self.in_suffix))
        if self.out_suffix != "default":
            self.add_args.append(os.path.join(kwargs['align_dir'], input + self.out_suffix))
        return


class Biobambam(BaseWrapper):
    '''
    Wrapper class to mark duplicates in a bam using biobambam
    '''

    # TODO: Clean up

    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        # TODO add update to input/output suffixes here
        if name.split("_")[0] == "bammarkduplicates2":
            self.update_file_suffix(input_default=".srtd.bam", output_default=".dup.srtd.bam", **kwargs)
        elif name.split("_")[0] == "bamsort":
            self.update_file_suffix(input_default=".bam", output_default=".srtd.bam", **kwargs)

        kwargs['target'] = input + self.out_suffix + "_" + hashlib.sha224(input + self.out_suffix).hexdigest() + ".txt"
        kwargs['stdout'] = os.path.join(kwargs['log_dir'], input + "_" + name + '.log')
        kwargs['prog_id'] = name
        name = self.prog_name_clean(name)

        self.init(name, **kwargs)

        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))
        else:
            self.job_parms.update({'mem': 10000, 'time': 300, 'ncpus': 1})

        self.args = ["index=0",
                     "I=" + os.path.join(self.align_dir, input + self.in_suffix),
                     "O=" + os.path.join(self.align_dir, input + self.out_suffix),
                     "M=" + os.path.join(self.qc_dir, input + ".dup.metrics.txt")]
        self.args += args
        self.setup_run()
        return




class QualiMap(BaseWrapper):
    """
    A wrapper for the running qualimap QC suite
    """

    cmd = ''

    # TODO: Clean up

    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        # TODO add update to input/output suffixes here
        self.update_file_suffix(input_default=".dup.srtd.bam", output_default="", **kwargs)

        kwargs['target'] = input + '.qualimapReport' + "_" + hashlib.sha224(
            input + '.qualimapReport.html').hexdigest() + ".txt"

        kwargs['stdout'] = os.path.join(kwargs['log_dir'], input + '_' + name + '.log')
        kwargs['prog_id'] = name
        name = self.prog_name_clean(name)

        new_name = name.split('_')[0]
        self.init(new_name, **kwargs)

        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))

            ## Update memory requirements for job if needed
            if 'mem' in kwargs.get('add_job_parms').keys():
                self.args += [' -Xmx' + str(kwargs.get('add_job_parms')['mem']) + 'M']
        else:
            # Set default memory options
            self.job_parms.update({'mem': 10000, 'time': 80, 'ncpus': 8})
            self.args += [' -Xmx10000M']
        self.args += [name.split('_')[1]]
        self.args += args

        if name.split("_")[1] == "rnaseq":
            gtf = kwargs.get('gtf_file')
            self.args += [" -gtf ", gtf]

        self.args += [" -bam ", os.path.join(kwargs.get('align_dir'), input + self.in_suffix),

                      " -outdir ", os.path.join(kwargs.get('work_dir'), "qc", input)]
        rename_results = ' '.join([" cp ", os.path.join(kwargs.get('qc_dir'), input, "qualimapReport.html "),
                                   os.path.join(kwargs.get('qc_dir'), input, input + "_qualimapReport.html ")])
        self.setup_run(add_command=rename_results)
        return


class SalmonCounts(BaseWrapper):
    """
    A wrapper for generating salmon counts

    """
    # TODO: Clean up
    cmd = ''
    args = []

    def __init__(self, name, input, *args, **kwargs):
        self.input = input

        kwargs['target'] = input + '.salmoncounts.' + hashlib.sha224(input + '.salmoncounts').hexdigest() + ".txt"
        new_name = name + " quant"
        self.init(new_name, **kwargs)

        # update job parameters if needed
        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))

        else:
            self.job_parms.update({'mem': 10000, 'time': 80, 'ncpus': 8})

        gtf = kwargs.get('gtf_file')
        self.args += args
        self.args += ["-l A"]

        if not self.paired_end:
            self.args += ["-r", os.path.join(kwargs.get('fastq_dir'), input + ".fq.gz")]
        else:
            self.args += ["-1", os.path.join(kwargs.get('fastq_dir'), input + "_1.fq.gz"),
                          "-2", os.path.join(kwargs.get('fastq_dir'), input + "_2.fq.gz")]

        self.args += [" -o " + os.path.join(kwargs.get('work_dir'), kwargs.get('expression_dir'),
                                            input + "_salmon_counts")]
        rename_results = ' '.join(
            [" cp ", os.path.join(kwargs.get('expression_dir'), input + "_salmon_counts", "quant.genes.sf"),
             os.path.join(kwargs.get('expression_dir'), input + "_salmon_quant.genes.txt")])
        self.setup_run(add_command=rename_results)
        return


class HtSeqCounts(BaseWrapper):
    """
    A wrapper for generating counts from HTSeq

    """
    # TODO: Clean up
    cmd = ''
    args = []

    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        # TODO add update to input/output suffixes here
        self.in_suffix = ".dup.srtd.bam"

        kwargs['target'] = input + '.htseqcounts.' + hashlib.sha224(input + '.htseqcounts').hexdigest() + ".txt"
        new_name = name
        kwargs['stderr'] = kwargs.get('stdout')
        kwargs.update({'stdout': os.path.join(kwargs.get('work_dir'), kwargs.get('expression_dir'),
                                              input + "_htseq_counts")})
        self.init(new_name, **kwargs)
        # update job parameters if needed
        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))
        else:
            self.job_parms.update({'mem': 10000, 'time': 80, 'ncpus': 2})

        gtf = kwargs.get('gtf_file')
        self.args += args
        self.args += ["-f", "bam", "-r", "pos", "-a", "0", "-t", "exon", "-i", "gene_id", "--additional-attr=gene_name",
                      "--nonunique=all", "--secondary-alignments=score"]
        self.args += [os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                      gtf]

        self.setup_run()
        return


class Bwa(BaseWrapper):
    """
    A wrapper for BWA

    """


    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        self.update_file_suffix(input_default=".fq.gz", output_default=".sam", **kwargs)

        ## set the checkpoint target file
        kwargs['target'] = input + self.out_suffix + "_" + hashlib.sha224(input + self.out_suffix).hexdigest() + ".txt"
        kwargs['stdout'] = os.path.join(kwargs['align_dir'], input + self.out_suffix)

        kwargs['prog_id'] = name
        name = self.prog_name_clean(name)

        new_name = ' '.join(name.split("_"))
        self.init(new_name, **kwargs)

        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))
            if 'ncpus' in kwargs.get('add_job_parms').keys():
                # TODO make sure threads are not given in the args
                self.args += [' -t ' + str(kwargs.get('add_job_parms')['ncpus'])]
        else:
            self.job_parms.update({'mem': 4000, 'time': 80, 'ncpus': 12})
            self.args += ['-t 12']

        # the below if block might be unecessary
        # if self.paired_end:
        #     kwargs['source'] = hashlib.sha224(input + '_2_fastqc.gzip').hexdigest() + ".txt"
        # else:
        #     kwargs['source'] = hashlib.sha224(input + '_fastqc.gzip').hexdigest() + ".txt"

        # self.setup_args()

        self.args += args

        if self.paired_end:
            self.args.append(os.path.join(self.cwd, 'fastq', input + "_1" + self.in_suffix))
            self.args.append(os.path.join(self.cwd, 'fastq', input + "_2" + self.in_suffix))
        else:
            self.args.append(os.path.join(self.cwd, 'fastq', input + self.in_suffix))
        # self.cmd = ' '.join(chain(self.cmd, map(str, self.args), map(str,input)))

        self.setup_run()
        return

    # def setup_args(self):
    #     self.args += ["--gunzip", "-A sam", "-N1", "--use-shared-memory=0"]
    #     return


class BedtoolsCounts(BaseWrapper):
    """
    A wrapper for bedtools to count RNAseq reads for Single End data

    """

    # TODO: Clean up

    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        kwargs['target'] = hashlib.sha224(input + '.bedtoolsCounts.csv').hexdigest() + ".txt"
        name = name + " multicov "
        self.init(name, **kwargs)
        self.args = ["-split", "-D", "-f 0.95",
                     "-a " + os.path.join(self.cwd, input + ".dup.srtd.bam"),
                     "-b " + os.path.join(self.cwd, input + ".dup.metrics.txt")]
        self.args += args
        self.setup_run()
        return


class FeatureCounts(BaseWrapper):
    """
     Wrapper for FeatureCounts
    """

    # TODO: Clean up

    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        # TODO add update to input/output suffixes here
        in_suffix = ".dup.srtd.bam"

        kwargs['target'] = hashlib.sha224(input + '.featureCounts.csv').hexdigest() + ".txt"
        # name = name + " multicov "
        kwargs['prog_id'] = name
        name = self.prog_name_clean(name)

        self.init(name, **kwargs)
        self.args = ["-split", "-D", "-f 0.95",
                     "-a " + os.path.join(self.cwd, input + self.in_suffix),
                     "-b " + os.path.join(self.cwd, input + ".dup.metrics.txt")]
        self.args += args
        self.setup_run()
        return


class FastqScreen(BaseWrapper):
    """
     Wrapper for fastqScreen
    """

    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        self.update_file_suffix(input_default='.fq.gz', output_default='', **kwargs)
        kwargs['target'] = input + "." + name + "_" + hashlib.sha224(input + name).hexdigest() + ".txt"

        if kwargs.get('paired_end'):
            kwargs['target'] = input + '.2.' + name + hashlib.sha224(input + '.2.' + name).hexdigest() + ".txt"

        kwargs['stdout'] = os.path.join(kwargs['log_dir'], input + "_" + name + ".log")
        kwargs['prog_id'] = name
        name = self.prog_name_clean(name)

        self.init(name, **kwargs)

        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))

            # Update threads if cpus given
            # TODO make sure threads are not given in the args
            if 'ncpus' in kwargs.get('add_job_parms').keys():
                self.args += [' --threads ' + str(kwargs.get('add_job_parms')['ncpus'])]
            else:
                # Set default memory and cpu options
                self.job_parms.update({'mem': 10000, 'time': 600, 'ncpus': 4})
                # TODO make sure threads are not given in the args
                self.args += ['--threads 4']

        fastq_screen_dir = os.path.join(kwargs.get('qc_dir'), "fastq_screen")

        if self.paired_end:
            self.args = ["--outdir ", fastq_screen_dir, "--force"]
            self.args += args
            self.args += [os.path.join(kwargs.get('fastq_dir'), input + "_1" + self.in_suffix)]
            # self.args.append(os.path.join(self.cwd, 'fastq', input + "_1.fq.gz"))
            self.setup_run()
            run_cmd1 = self.run_command

            ## Re initialize the object for the second pair
            self.init(name, **kwargs)
            self.args = ["--outdir ", fastq_screen_dir, "--force"]
            self.args += args
            self.args += [os.path.join(kwargs.get('fastq_dir'), input + "_1" + self.in_suffix)]
            self.setup_run()
            run_cmd2 = self.run_command

            self.run_command = run_cmd1 + "; " + run_cmd2
        else:
            self.args = ["--outdir ", fastq_screen_dir, "--force"]
            self.args += args
            self.args += [os.path.join(kwargs.get('fastq_dir'), input + "_1" + self.in_suffix)]
            self.args += [os.path.join(kwargs.get('fastq_dir'), input + "_2" + self.in_suffix)]
            self.setup_run()

        self.run_command = "mkdir -pv " + fastq_screen_dir + ";" + self.run_command
        return


class Trimmomatic(BaseWrapper):
    """
        Wrapper for trimmomatic
    """

    def __init__(self, name, input, *args, **kwargs):
        print "Printing trimmomatic args"
        print args
        self.input = input

        kwargs['prog_id'] = name
        name = self.prog_name_clean(name)

        new_name = ' '.join(name.split("_"))

        # TODO add update to input/output suffixes here
        self.update_file_suffix(input_default=".fq.gz", output_default="_tr.fq.gz", **kwargs)

        kwargs['stdout'] = os.path.join(kwargs['log_dir'], input + "_" + kwargs['prog_id'] + ".log")

        self.init(new_name, **kwargs)

        if kwargs.get('job_parms_type') != 'default':
            # self.job_parms.update(dict(kwargs.get('add_job_parms')))
            self.job_parms.update(kwargs.get('add_job_parms'))

            # Update threads if cpus given ## Need to fix this based on environment o
            # Other parameters. this can break when slurm config does not allow multi
            # threads per cpu
            # TODO make sure threads are not given in the args

            if 'ncpus' in kwargs.get('add_job_parms').keys():
                self.args += [' -threads ' + str(kwargs.get('add_job_parms')['ncpus'])]

        else:
            # Set default memory and cpu options
            self.job_parms.update({'mem': 10000, 'time': 600, 'ncpus': 4})
            # TODO make sure threads are not given in the args
            self.args += ['-threads 4']

        self.args += ["-trimlog", os.path.join(kwargs.get('log_dir'), input + "_" + name + ".log")]
        add_command = ''
        if self.paired_end:

            self.args += [os.path.join(kwargs.get('fastq_dir'), input + "_1" + self.in_suffix),
                          os.path.join(kwargs.get('fastq_dir'), input + "_2" + self.in_suffix)]

            add_command = "mv -v " + os.path.join(kwargs.get('fastq_dir'), input + "_tr_1P.fq.gz") + " "
            add_command += os.path.join(kwargs.get('fastq_dir'), input + "_1" + self.out_suffix) + "; "
            add_command += "mv -v " + os.path.join(kwargs.get('fastq_dir'), input + "_tr_2P.fq.gz") + " "
            add_command += os.path.join(kwargs.get('fastq_dir'), input + "_2" + self.out_suffix) + "; "
            add_command += "rm -v " + os.path.join(kwargs.get('fastq_dir'), input + "_tr_1U.fq.gz") + "; "
            add_command += "rm -v " + os.path.join(kwargs.get('fastq_dir'), input + "_tr_2U.fq.gz") + "; "
        else:
            self.args += [os.path.join(kwargs.get('fastq_dir'), input + self.in_suffix)]
            # Todo need to check what move commands are added for SingleEnd

        self.args += ["-baseout", os.path.join(kwargs.get('fastq_dir'), input + self.out_suffix)]

        # Add all other optional trimming specification arguments
        self.args += args
        self.setup_run(add_command=add_command)

        return

class Picard(BaseWrapper):
    """
        A wrapper for picardtools
        picard CollectWgsMetrics \
        INPUT=$mysamplebase"_sorted.bam" \ OUTPUT=$mysamplebase"_stats_picard.txt"\
        REFERENCE_SEQUENCE=$myfasta \
        MINIMUM_MAPPING_QUALITY=20 \
        MINIMUM_BASE_QUALITY=20 \
        VALIDATION_STRINGENCY=LENIENT
    """


    def __init__(self, name, input, *args, **kwargs):
        self.input = input
        print "Printing optional arguments " + name
        print args
        # TODO add remove duprun function

        kwargs['prog_id'] = name
        # Remove the round from the name
        name = self.prog_name_clean(name)

        ## set the checkpoint target file
        new_name = ' '.join(name.split("_"))

        # kwargs['target'] = input + '._wgs_stats_picard.' + hashlib.sha224(input + '._wgs_stats_picard.txt').hexdigest() + ".txt"
        self.make_target(name, input, *args, **kwargs)

        kwargs['target'] = self.target
        self.init(new_name, **kwargs)

        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))

            ## Update threads if cpus given
            # if 'ncpus' in kwargs.get('add_job_parms').keys():
            #     self.args += [' -t ' + str(kwargs.get('add_job_parms')['ncpus'])]

            ## Update memory requirements  if needed
            if 'mem' in kwargs.get('add_job_parms').keys():
                self.args += [' -Xmx' + str(kwargs.get('add_job_parms')['mem']) + 'M']

        else:
            # Set default memory and cpu options
            self.job_parms.update({'mem': 10000, 'time': 80, 'ncpus': 4})
            self.args += [' -Xmx10000M']

        kwargs['source'] = input + self.in_suffix + hashlib.sha224(input + self.in_suffix).hexdigest() + ".txt"
        self.args += self.add_args

        self.setup_run()
        return

    def make_target(self, name, input, *args, **kwargs):
        if name.split('_')[1] == "CollectWgsMetrics":
            self.update_file_suffix(input_default=".dup.srtd.bam", output_default='_wgs_stats_picard.txt', **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_collect_wgs_metrics(input, *args, **kwargs)
        elif name.split('_')[1] == "MeanQualityByCycle":
            self.update_file_suffix(input_default=".dup.srtd.bam", output_default='_read_qual_by_cycle_picard',
                                    **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix + ".txt").hexdigest() + ".txt"
            self.add_args_mean_quality_by_cycle(input, *args, **kwargs)
        elif name.split('_')[1] == "QualityScoreDistribution":
            self.update_file_suffix(input_default=".dup.srtd.bam", output_default='_read_qual_overall_picard', **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix + ".txt").hexdigest() + ".txt"
            self.add_args_quality_score_distribution(input, *args, **kwargs)
        elif name.split('_')[1] == "MarkDuplicates":
            self.update_file_suffix(input_default=".rg.srtd.bam", output_default=".rg.srtd.bam", **kwargs)
            self.target = input + '_mark_dup_picard.txt' + "_" + hashlib.sha224(
                input + '_mark_dup_picard.txt').hexdigest() + ".txt"
            self.add_args_markduplicates(input, *args, **kwargs)
        elif name.split('_')[1] == "AddOrReplaceReadGroups":
            self.update_file_suffix(input_default=".dup.srtd.bam", output_default=".rg.srtd.bam", **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_addorreplacereadgroups(input, *args, **kwargs)
        elif name.split('_')[1] == "BuildBamIndex":
            self.update_file_suffix(input_default=".gatk.recal.bam", output_default="", **kwargs)
            self.target = input + self.in_suffix + ".bai_" + hashlib.sha224(
                input + self.in_suffix + '.bai').hexdigest() + ".txt"
            self.add_args_buildbamindex(input, *args, **kwargs)
        return

    def add_args_collect_wgs_metrics(self, input, *args, **kwargs):
        self.reset_add_args()
        self.add_args = ["INPUT=" + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "OUTPUT=" + os.path.join(kwargs.get('qc_dir'), input + self.out_suffix),
                         "REFERENCE_SEQUENCE=" + kwargs.get("ref_fasta_path"),
                         "MINIMUM_MAPPING_QUALITY=20", "MINIMUM_BASE_QUALITY=20",
                         "COUNT_UNPAIRED=true", "VALIDATION_STRINGENCY=LENIENT"]
        self.add_args += args
        return

    def add_args_mean_quality_by_cycle(self, input, *args, **kwargs):
        self.reset_add_args()

        self.add_args = ["INPUT=" + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "OUTPUT=" + os.path.join(kwargs.get('qc_dir'), input + + self.out_suffix + '.txt'),
                         "REFERENCE_SEQUENCE=" + kwargs.get("ref_fasta_path"),
                         "CHART_OUTPUT=" + os.path.join(kwargs.get('qc_dir'), input + self.out_suffix + '.pdf'),
                         "VALIDATION_STRINGENCY=LENIENT"]
        self.add_args += args
        return

    def add_args_quality_score_distribution(self, input, *args, **kwargs):
        self.reset_add_args()

        self.add_args = ["INPUT=" + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "OUTPUT=" + os.path.join(kwargs.get('qc_dir'), input + self.out_suffix + '.txt'),
                         "REFERENCE_SEQUENCE=" + kwargs.get("ref_fasta_path"),
                         "CHART_OUTPUT=" + os.path.join(kwargs.get('qc_dir'), input + self.out_suffix + '.pdf'),
                         "VALIDATION_STRINGENCY=LENIENT"]
        self.add_args += args
        return

    def add_args_addorreplacereadgroups(self, input, *args, **kwargs):
        self.reset_add_args()

        self.add_args = ["INPUT=" + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "OUTPUT=" + os.path.join(kwargs.get('align_dir'), input + self.out_suffix),
                         "RGID=" + input,
                         "RGLB=lib1 RGPL=illumina  RGPU=unit1 RGCN=BGI",
                         "RGSM=" + input,
                         "VALIDATION_STRINGENCY=LENIENT"]
        self.add_args += args
        return

    def add_args_markduplicates(self, input, *args, **kwargs):
        self.reset_add_args()

        self.add_args = ["INPUT=" + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "M=" + os.path.join(kwargs.get('qc_dir'), input + '_mark_duplicates_picard.txt'),
                         "CREATE_INDEX=true VALIDATION_STRINGENCY=LENIENT"
                         ]
        if "REMOVE_DUPLICATES=true" in args:
            # TODO add update to input/output suffixes here
            self.out_suffix = ".dedup" + self.out_suffix

            self.add_args += ["OUTPUT=" + os.path.join(kwargs.get('align_dir'), input + self.out_suffix)]
        else:
            # TODO add update to input/output suffixes here
            self.out_suffix = ".picdup" + self.out_suffix

            self.add_args += ["OUTPUT=" + os.path.join(kwargs.get('align_dir'), input + self.out_suffix)]

        self.add_args += args
        return

    def add_args_buildbamindex(self, input, *args, **kwargs):
        self.reset_add_args()

        self.add_args = ["INPUT=" + os.path.join(kwargs.get('align_dir'),
                                                 input + self.in_suffix),
                         "VALIDATION_STRINGENCY=LENIENT"]
        self.add_args += args
        return


class Gatk(BaseWrapper):
    """
        A wrapper for picardtools
        picard CollectWgsMetrics \
        INPUT=$mysamplebase"_sorted.bam" \ OUTPUT=$mysamplebase"_stats_picard.txt"\
        REFERENCE_SEQUENCE=$myfasta \
        MINIMUM_MAPPING_QUALITY=20 \
        MINIMUM_BASE_QUALITY=20 \
        VALIDATION_STRINGENCY=LENIENT
    """

    def __init__(self, name, input, *args, **kwargs):

        self.input = input

        kwargs['prog_id'] = name
        # Remove the round from the name
        name = self.prog_name_clean(name)

        ## set the checkpoint target file

        self.make_target(name, input, *args, **kwargs)
        kwargs['target'] = self.target
        kwargs['stdout'] = self.stdout

        # We have to set name and run init so mem_str can be updated
        # from the yaml and then we re-ddo name and init a second time
        new_name = ' '.join(name.split("_"))
        self.init(new_name, **kwargs)

        mem_str = ' -Xmx10000M'
        if kwargs.get('job_parms_type') != 'default':
            self.job_parms.update(kwargs.get('add_job_parms'))
            # TODO: figure how how to update threads for GATK based on CPUS?
            ## Update memory requirements  for Java
            if 'mem' in kwargs.get('add_job_parms').keys():
                # self.args += [' -Xmx' + str(kwargs.get('add_job_parms')['mem']) + 'M']
                mem_str = ' -Xmx' + str(kwargs.get('add_job_parms')['mem']) + 'M'
        else:
            # Set default memory and cpu options
            self.job_parms.update({'mem': 10000, 'time': 80, 'ncpus': 4})

        new_name = name.split("_")
        new_name.insert(1, mem_str)
        new_name.insert(2, '-T')
        new_name = ' '.join(new_name)

        self.init(new_name, **kwargs)

        # kwargs['source'] = input + '.dedup.srtd.bam' + hashlib.sha224(input + '.dedup.rg.srtd.bam').hexdigest() + ".txt"
        # ref_fasta = kwargs.get("ref_fasta_path")
        # self.args += args

        # Note: We add all optional arguments to the end of the add_args variable

        self.args += self.add_args
        # self.args += ["INPUT=" + os.path.join(kwargs.get('align_dir'), input + ".dup.srtd.bam"),
        #               "OUTPUT=" + os.path.join(kwargs.get('qc_dir'),input + '._wgs_stats_picard.txt'),
        #               "REFERENCE_SEQUENCE=" + kwargs.get("ref_fasta_path"),
        #               "MINIMUM_MAPPING_QUALITY="+"20","MINIMUM_BASE_QUALITY="+"20",
        #               "COUNT_UNPAIRED=true", "VALIDATION_STRINGENCY="+"LENIENT"]

        self.setup_run()
        return

    def make_target(self, name, input, *args, **kwargs):
        if name.split('_')[1] == "RealignerTargetCreator":
            self.update_file_suffix(input_default=".dedup.rg.srtd.bam", output_default='_realign_targets.intervals',
                                    **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_realigner_target_creator(input, *args, **kwargs)

        elif name.split('_')[1] == "IndelRealigner":
            self.update_file_suffix(input_default=".dedup.rg.srtd.bam", output_default='.dedup.rg.srtd.realigned.bam',
                                    **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_indel_realigner(input, *args, **kwargs)

        elif name.split('_')[1] == "BaseRecalibrator":

            self.update_file_suffix(input_default='.dedup.rg.srtd.realigned.bam', output_default='_recal_table.txt',
                                    **kwargs)
            if "-BQSR" in args:
                self.target = input + '_post' + self.out_suffix + "_" + hashlib.sha224(
                    input + '_post' + self.out_suffix).hexdigest() + ".txt"
            else:
                self.target = input + self.out_suffix + "_" + hashlib.sha224(
                    input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_base_recalibrator(input, *args, **kwargs)

        elif name.split('_')[1] == "PrintReads":
            self.update_file_suffix(input_default='.dedup.rg.srtd.realigned.bam', output_default='.gatk.recal.bam',
                                    **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_print_reads(input, *args, **kwargs)

        elif name.split('_')[1] == "HaplotypeCaller":
            self.update_file_suffix(input_default='.gatk.recal.bam', output_default='.GATK-HC.g.vcf', **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_haplotype_caller(input, *args, **kwargs)

        elif name.split('_')[1] == "VariantRecalibrator":
            self.target = input + '._mark_dup_picard.' + hashlib.sha224(
                input + '._mark_dup_picard.txt').hexdigest() + ".vcf"

        elif name.split('_')[1] == "AnalyzeCovariates":
            self.update_file_suffix(input_default="_recal_table.txt", output_default="_recalibration_plots.pdf",
                                    **kwargs)
            self.target = input + self.out_suffix + "_" + hashlib.sha224(
                input + self.out_suffix).hexdigest() + ".txt"
            self.add_args_analyze_covariates(input, *args, **kwargs)
        return

    def add_args_realigner_target_creator(self, input, *args, **kwargs):
        # gatk -Xmx20G -T RealignerTargetCreator -R $my.fasta -I $my.bam\
        #  -known /gpfs/data/cbc/references/ftp.broadinstitute.org/bundle/hg19/Mills_and_1000G_gold_standard.indels.hg19.sites.vcf \
        # -o $samp_realign_targets.intervals
        # kwargs.get()
        self.stdout = os.path.join(kwargs['log_dir'], input + "_" + kwargs['prog_id'] + '.log')
        self.reset_add_args()

        self.add_args = ["-I " + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "-o " + os.path.join(kwargs.get('gatk_dir'), input + self.out_suffix),
                         "-R " + kwargs.get("ref_fasta_path")]

        self.add_args += args
        # TODO Dont need the below.. Add an exception handler to make sure at least one -known is present
        # self.add_args += [
        #     "-known " + "/gpfs/data/cbc/references/ftp.broadinstitute.org/bundle/hg19/Mills_and_1000G_gold_standard.indels.hg19.sites.vcf"]

        return

    def add_args_indel_realigner(self, input, *args, **kwargs):
        # gatk - T IndelRealigner \
        # - R $myfasta \
        # - known $myindel \
        # - targetIntervals $mysamplebase"_realign_targets.intervals" \
        # - I $mysamplebase"_sorted_dedup.bam" \
        # - o $mysamplebase"_sorted_dedup_realigned.bam" \

        self.stdout = os.path.join(kwargs['log_dir'], input + "_" + kwargs['prog_id'] + '.log')
        self.reset_add_args()

        self.add_args = ["-I " + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "-o " + os.path.join(kwargs.get('align_dir'), input + self.out_suffix),
                         "-R " + kwargs.get("ref_fasta_path"),
                         "-targetIntervals " + os.path.join(kwargs.get('gatk_dir'),
                                                            input + '_realign_targets.intervals')
                         ]
        self.add_args += args

        return

    def add_args_base_recalibrator(self, input, *args, **kwargs):
        # gatk - T IndelRealigner \
        # - R $myfasta \
        # - known $myindel \
        # - targetIntervals $mysamplebase"_realign_targets.intervals" \
        # - I $mysamplebase"_sorted_dedup.bam" \
        # - o $mysamplebase"_sorted_dedup_realigned.bam" \

        # kwargs.get()

        self.stdout = os.path.join(kwargs['log_dir'], input + "_" + kwargs['prog_id'] + '.log')
        self.reset_add_args()

        self.add_args = ["-I " + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "-R " + kwargs.get("ref_fasta_path")]

        # TODO make this optional by searching *args and replacing and an exception handler to ensure at least one -knownSites is present
        # self.add_args += [" -ncgt 8"]
        # self.add_args += [
        #    "-knownSites " + "/gpfs/data/cbc/references/ftp.broadinstitute.org/bundle/hg19/Mills_and_1000G_gold_standard.indels.hg19.sites.vcf"]
        # self.add_args += [
        #    "-knownSites " + "/gpfs/data/cbc/references/ftp.broadinstitute.org/bundle/hg19/dbsnp_138.hg19.vcf"]
        print "Printing optional arguments"
        print args
        if "-BQSR" in args:
            self.stdout = os.path.join(kwargs['log_dir'], input + "_" + kwargs['prog_id'] + '.log')
            new_args = list(args)
            idx_to_rm = [i for i, s in enumerate(new_args) if '-BQSR' in s][0]
            del new_args[idx_to_rm]
            self.add_args += new_args
            # self.out_suffix is used as input and the output has '_post` appended to it
            self.add_args += ["-BQSR " + os.path.join(kwargs.get('gatk_dir'), input + self.out_suffix),
                              "-o " + os.path.join(kwargs.get('gatk_dir'), input + "_post" + self.out_suffix)]
        else:
            self.add_args += args
            self.add_args += ["-o " + os.path.join(kwargs.get('gatk_dir'), input + self.out_suffix)]

        return

    def add_args_print_reads(self, input, *args, **kwargs):
        # gatk - Xmx30G - T PrintReads
        # -R /gpfs/data/cbc/references/ftp.broadinstitute.org/bundle/hg19/ucsc.hg19.fasta
        # -I /gpfs/data/cbc/uzun/wes_analysis/wes_run_1/gatk_all_run/WESPE2932_dedup_rg_realigned.bam
        # -BQSR /gpfs/data/cbc/uzun/wes_analysis/wes_run_1/gatk_all_run/WESPE2932_recal_table.txt
        # -o /gpfs/data/cbc/uzun/wes_analysis/wes_run_1/gatk_all_run/WESPE2932_recal_gatk.bam

        self.stdout = os.path.join(kwargs['log_dir'], input + "_" + kwargs['prog_id'] + '.log')
        self.reset_add_args()

        self.add_args = [
            "-I " + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
            "-R " + kwargs.get("ref_fasta_path"),
            "-BQSR " + os.path.join(kwargs.get('gatk_dir'), input + "_recal_table.txt"),
            "-o " + os.path.join(kwargs.get('align_dir'), input + self.out_suffix)
        ]
        return

    def add_args_haplotype_caller(self, input, *args, **kwargs):
        # gatk -Xmx30G -T HaplotypeCaller
        # -nct 4
        # -R /gpfs/data/cbc/references/ftp.broadinstitute.org/bundle/hg19/ucsc.hg19.fasta
        # -I /gpfs/data/cbc/uzun/wes_analysis/wes_run_1/gatk_all_run/WESPE2932_recal_gatk.bam
        # --dbsnp /gpfs/data/cbc/references/ftp.broadinstitute.org/bundle/hg19/dbsnp_138.hg19.vcf
        # -stand_call_conf 30
        # --genotyping_mode DISCOVERY
        # --emitRefConfidence GVCF
        # -o /gpfs/data/cbc/uzun/wes_analysis/wes_run_1/gatk_all_run/WESPE2932_GATK-HC.g.vcf

        # kwargs.get()

        self.stdout = os.path.join(kwargs['log_dir'], input + "_" + kwargs['prog_id'] + '.log')
        self.reset_add_args()

        self.add_args = ["-I " + os.path.join(kwargs.get('align_dir'), input + self.in_suffix),
                         "-R " + kwargs.get("ref_fasta_path")]
        self.add_args += args
        self.add_args += ["-o " + os.path.join(kwargs.get('gatk_dir'), input + self.out_suffix)]
        return


    def add_args_analyze_covariates(self, input, *args, **kwargs):
        self.stdout = os.path.join(kwargs['log_dir'], input + "_" + kwargs['prog_id'] + '.log')
        self.reset_add_args()

        self.add_args = ["-R " + kwargs.get("ref_fasta_path"),
                         "-before " + os.path.join(kwargs.get('gatk_dir'), input + self.in_suffix),
                         "-after " + os.path.join(kwargs.get('gatk_dir'), input + "_post" + self.in_suffix),
                         "-plots " + os.path.join(kwargs.get('gatk_dir'), input + self.out_suffix)
                         ]
        return
