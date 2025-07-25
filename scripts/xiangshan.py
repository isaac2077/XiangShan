#***************************************************************************************
# Copyright (c) 2024 Beijing Institute of Open Source Chip (BOSC)
# Copyright (c) 2020-2024 Institute of Computing Technology, Chinese Academy of Sciences
# Copyright (c) 2020-2021 Peng Cheng Laboratory
#
# XiangShan is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
#
# See the Mulan PSL v2 for more details.
#***************************************************************************************

# Simple version of xiangshan python wrapper

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
import shlex
import psutil
import re

def find_files_with_suffix(root_dir, suffixes):
    matching_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            if any(filename.endswith(suffix) for suffix in suffixes):
                absolute_path = os.path.join(dirpath, filename)
                matching_files.append(absolute_path)
    return matching_files

def load_all_gcpt(gcpt_paths):
    all_gcpt = []
    for gcpt_path in gcpt_paths:
        all_gcpt.extend(find_files_with_suffix(gcpt_path, ['.zstd', '.gz']))
    return all_gcpt

class XSArgs(object):
    script_path = os.path.realpath(__file__)
    # default path to the repositories
    noop_home = os.path.join(os.path.dirname(script_path), "..")
    nemu_home = os.path.join(noop_home, "../NEMU")
    am_home = os.path.join(noop_home, "../nexus-am")
    dramsim3_home = os.path.join(noop_home, "../DRAMsim3")
    rvtest_home = os.path.join(noop_home, "../riscv-tests")
    default_wave_home = os.path.join(noop_home, "build")
    wave_home   = default_wave_home

    def __init__(self, args):
        # all path environment variables that should be set
        all_path = [
            # (python argument, environment variable, default, target function)
            (None, "NOOP_HOME", self.noop_home, self.set_noop_home),
            (args.nemu, "NEMU_HOME", self.nemu_home, self.set_nemu_home),
            (args.am, "AM_HOME", self.am_home, self.set_am_home),
            (args.dramsim3, "DRAMSIM3_HOME", self.dramsim3_home, self.set_dramsim3_home),
            (args.rvtest, "RVTEST_HOME", self.rvtest_home, self.set_rvtest_home),
        ]
        for (arg_in, env, default, set_func) in all_path:
            set_func(self.__extract_path(arg_in, env, default))
        # Chisel arguments
        self.enable_log = args.enable_log
        self.num_cores = args.num_cores
        # Makefile arguments
        self.threads = args.threads
        self.make_threads = args.make_threads
        self.with_dramsim3 = 1 if args.with_dramsim3 else None
        self.is_release = 1 if args.release else None
        self.is_spike = "Spike" if args.spike else None
        self.trace = 1 if args.trace or not args.disable_fork and not args.trace_fst else None
        self.trace_fst = "fst" if args.trace_fst else None
        self.config = args.config
        self.yaml_config = args.yaml_config
        self.emu_optimize = args.emu_optimize
        self.xprop = 1 if args.xprop else None
        self.issue = args.issue
        self.with_chiseldb = 0 if args.no_db else 1
        # emu arguments
        self.max_instr = args.max_instr
        self.ram_size = args.ram_size
        self.seed = random.randint(0, 9999)
        self.numa = args.numa
        self.diff = args.diff
        if args.spike and "nemu" in args.diff:
            self.diff = self.diff.replace("nemu-interpreter", "spike")
        self.fork = not args.disable_fork
        self.disable_diff = args.no_diff
        self.disable_db = args.no_db
        self.gcpt_restore_bin = args.gcpt_restore_bin
        self.pgo = args.pgo
        self.pgo_max_cycle = args.pgo_max_cycle
        self.pgo_emu_args = args.pgo_emu_args
        self.llvm_profdata = args.llvm_profdata
        # wave dump path
        if args.wave_dump is not None:
            self.set_wave_home(args.wave_dump)
        else:
            self.set_wave_home(self.default_wave_home)

    def get_env_variables(self):
        all_env = {
            "NOOP_HOME"    : self.noop_home,
            "NEMU_HOME"    : self.nemu_home,
            "WAVE_HOME"    : self.wave_home,
            "AM_HOME"      : self.am_home,
            "DRAMSIM3_HOME": self.dramsim3_home,
            "MODULEPATH": "/usr/share/Modules/modulefiles:/etc/modulefiles"
        }
        return all_env

    def get_chisel_args(self, prefix=None):
        chisel_args = [
            (self.enable_log, "enable-log")
        ]
        args = map(lambda x: x[1], filter(lambda arg: arg[0], chisel_args))
        if prefix is not None:
            args = map(lambda x: prefix + x, args)
        return args

    def get_makefile_args(self):
        makefile_args = [
            (self.threads,       "EMU_THREADS"),
            (self.with_dramsim3, "WITH_DRAMSIM3"),
            (self.is_release,    "RELEASE"),
            (self.is_spike,      "REF"),
            (self.trace,         "EMU_TRACE"),
            (self.trace_fst,     "EMU_TRACE"),
            (self.config,        "CONFIG"),
            (self.num_cores,     "NUM_CORES"),
            (self.emu_optimize,  "EMU_OPTIMIZE"),
            (self.xprop,         "ENABLE_XPROP"),
            (self.with_chiseldb, "WITH_CHISELDB"),
            (self.yaml_config,   "YAML_CONFIG"),
            (self.pgo,           "PGO_WORKLOAD"),
            (self.pgo_max_cycle, "PGO_MAX_CYCLE"),
            (self.pgo_emu_args,  "PGO_EMU_ARGS"),
            (self.llvm_profdata, "LLVM_PROFDATA"),
            (self.issue,         "ISSUE"),
        ]
        args = filter(lambda arg: arg[0] is not None, makefile_args)
        args = [(shlex.quote(str(arg[0])), arg[1]) for arg in args] # shell escape
        return args

    def get_emu_args(self):
        emu_args = [
            (self.max_instr, "max-instr"),
            (self.diff,      "diff"),
            (self.seed,      "seed"),
            (self.ram_size,  "ram-size"),
        ]
        args = filter(lambda arg: arg[0] is not None, emu_args)
        return args

    def show(self):
        print("Extra environment variables:")
        env = self.get_env_variables()
        for env_name in env:
            print(f"{env_name}: {env[env_name]}")
        print()
        print("Chisel arguments:")
        print(" ".join(self.get_chisel_args()))
        print()
        print("Makefile arguments:")
        for val, name in self.get_makefile_args():
            print(f"{name}={val}")
        print()
        print("emu arguments:")
        for val, name in self.get_emu_args():
            print(f"--{name} {val}")
        print()

    def __extract_path(self, path, env=None, default=None):
        if path is None and env is not None:
            path = os.getenv(env)
        if path is None and default is not None:
            path = default
        path = os.path.realpath(path)
        return path

    def set_noop_home(self, path):
        self.noop_home = path

    def set_nemu_home(self, path):
        self.nemu_home = path

    def set_am_home(self, path):
        self.am_home = path

    def set_dramsim3_home(self, path):
        self.dramsim3_home = path

    def set_rvtest_home(self, path):
        self.rvtest_home = path

    def set_wave_home(self, path):
        print(f"set wave home to {path}")
        self.wave_home = path

# XiangShan environment
class XiangShan(object):
    def __init__(self, args):
        self.args = XSArgs(args)
        self.timeout = args.timeout

    def show(self):
        self.args.show()

    def make_clean(self):
        print("Clean up CI workspace")
        self.show()
        return_code = self.__exec_cmd(f'make -C $NOOP_HOME clean')
        return return_code

    def generate_verilog(self):
        print("Generating XiangShan verilog with the following configurations:")
        self.show()
        sim_args = " ".join(self.args.get_chisel_args(prefix="--"))
        make_args = " ".join(map(lambda arg: f"{arg[1]}={arg[0]}", self.args.get_makefile_args()))
        return_code = self.__exec_cmd(f'make -C $NOOP_HOME verilog SIM_ARGS="{sim_args}" {make_args}')
        return return_code

    def generate_sim_verilog(self):
        print("Generating XiangShan sim-verilog with the following configurations:")
        self.show()
        sim_args = " ".join(self.args.get_chisel_args(prefix="--"))
        make_args = " ".join(map(lambda arg: f"{arg[1]}={arg[0]}", self.args.get_makefile_args()))
        return_code = self.__exec_cmd(f'make -C $NOOP_HOME sim-verilog SIM_ARGS="{sim_args}" {make_args}')
        return return_code

    def build_emu(self):
        print("Building XiangShan emu with the following configurations:")
        self.show()
        sim_args = " ".join(self.args.get_chisel_args(prefix="--"))
        make_args = " ".join(map(lambda arg: f"{arg[1]}={arg[0]}", self.args.get_makefile_args()))
        threads = self.args.make_threads
        return_code = self.__exec_cmd(f'make -C $NOOP_HOME emu -j{threads} SIM_ARGS="{sim_args}" {make_args}')
        return return_code

    def build_simv(self):
        print("Building XiangShan simv with the following configurations")
        self.show()
        make_args = " ".join(map(lambda arg: f"{arg[1]}={arg[0]}", self.args.get_makefile_args()))
        # TODO: make the following commands grouped as unseen scripts
        return_code = self.__exec_cmd(f'\
            eval `/usr/bin/modulecmd zsh load license`;\
            eval `/usr/bin/modulecmd zsh load synopsys/vcs/Q-2020.03-SP2`;\
            eval `/usr/bin/modulecmd zsh load synopsys/verdi/S-2021.09-SP1`;\
            VERDI_HOME=/nfs/tools/synopsys/verdi/S-2021.09-SP1 \
            make -C $NOOP_HOME simv {make_args} CONSIDER_FSDB=1')  # set CONSIDER_FSDB for compatibility
        return return_code

    def run_emu(self, workload):
        print("Running XiangShan emu with the following configurations:")
        self.show()
        emu_args = " ".join(map(lambda arg: f"--{arg[1]} {arg[0]}", self.args.get_emu_args()))
        print("workload:", workload)
        numa_args = ""
        if self.args.numa:
            numa_info = get_free_cores(self.args.threads)
            numa_args = f"numactl -m {numa_info[0]} -C {numa_info[1]}-{numa_info[2]}"
        fork_args = "--enable-fork" if self.args.fork else ""
        diff_args = "--no-diff" if self.args.disable_diff else ""
        chiseldb_args = "--dump-db" if not self.args.disable_db else ""
        gcpt_restore_args = f"-r {self.args.gcpt_restore_bin}" if len(self.args.gcpt_restore_bin) != 0 else ""
        return_code = self.__exec_cmd(f'ulimit -s {32 * 1024}; {numa_args} $NOOP_HOME/build/emu -i {workload} {emu_args} {fork_args} {diff_args} {chiseldb_args} {gcpt_restore_args}')
        return return_code

    def run_simv(self, workload):
        print("Running XiangShan simv with the following configurations:")
        self.show()
        diff_args = "$NOOP_HOME/"+ args.diff
        assert_args = "-assert finish_maxfail=30 -assert global_finish_maxfail=10000"
        return_code = self.__exec_cmd(f'cd $NOOP_HOME/build && ./simv +workload={workload} +diff={diff_args} +dump-wave=fsdb {assert_args} | tee simv.log')
        with open(f"{self.args.noop_home}/build/simv.log") as f:
            content = f.read()
            if "Offending" in content or "HIT GOOD TRAP" not in content:
                return 1
        return return_code

    def run(self, args):
        if args.ci is not None:
            return self.run_ci(args.ci)
        if args.ci_vcs is not None:
            return self.run_ci_vcs(args.ci_vcs)
        actions = [
            (args.generate, lambda _ : self.generate_verilog()),
            (args.vcs_gen, lambda _ : self.generate_sim_verilog()),
            (args.build, lambda _ : self.build_emu()),
            (args.vcs_build, lambda _ : self.build_simv()),
            (args.workload, lambda args: self.run_emu(args.workload)),
            (args.clean, lambda _ : self.make_clean())
        ]
        valid_actions = map(lambda act: act[1], filter(lambda act: act[0], actions))
        for i, action in enumerate(valid_actions):
            print(f"Action {i}:")
            ret = action(args)
            if ret:
                return ret
        return 0

    def __exec_cmd(self, cmd):
        env = dict(os.environ)
        env.update(self.args.get_env_variables())
        print("subprocess call cmd:", cmd)
        start = time.time()
        proc = subprocess.Popen(cmd, shell=True, env=env, preexec_fn=os.setsid)
        try:
            return_code = proc.wait(self.timeout)
            end = time.time()
            print(f"Elapsed time: {end - start} seconds")
            return return_code
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            print(f"KeyboardInterrupt or TimeoutExpired.")
            return 0

    def __get_ci_cputest(self, name=None):
        # base_dir = os.path.join(self.args.am_home, "tests/cputest/build")
        base_dir = "/nfs/home/share/ci-workloads/nexus-am-workloads/tests/cputest"
        cputest = os.listdir(base_dir)
        cputest = filter(lambda x: x.endswith(".bin"), cputest)
        cputest = map(lambda x: os.path.join(base_dir, x), cputest)
        return cputest

    def __get_ci_rvtest(self, name=None):
        base_dir = os.path.join(self.args.rvtest_home, "isa/build")
        riscv_tests = os.listdir(base_dir)
        riscv_tests = filter(lambda x: x.endswith(".bin"), riscv_tests)
        all_rv_tests = ["rv64ui", "rv64um", "rv64ua", "rv64uf", "rv64ud", "rv64mi"]
        riscv_tests = filter(lambda x: x[:6] in all_rv_tests, riscv_tests)
        riscv_tests = map(lambda x: os.path.join(base_dir, x), riscv_tests)
        return riscv_tests

    def __get_ci_misc(self, name=None):
        base_dir = "/nfs/home/share/ci-workloads"
        workloads = [
            "bitmanip/bitMisc.bin",
            "crypto/crypto-riscv64-noop.bin",
            # "coremark_rv64gc_o2/coremark-riscv64-xs.bin",
            # "coremark_rv64gc_o3/coremark-riscv64-xs.bin",
            # "coremark_rv64gcb_o3/coremark-riscv64-xs.bin",
            "nexus-am-workloads/amtest/external_intr-riscv64-xs.bin",
            "nexus-am-workloads/tests/aliastest/aliastest-riscv64-xs.bin",
            "Svinval/rv64mi-p-svinval.bin",
            "pmp/pmp.riscv.bin",
            "nexus-am-workloads/amtest/pmp_test-riscv64-xs.bin",
            "nexus-am-workloads/amtest/sv39_hp_atom_test-riscv64-xs.bin",
            "asid/asid.bin",
            "isa_misc/xret_clear_mprv.bin",
            "isa_misc/satp_ppn.bin",
            "cache-management/softprefetchtest-riscv64-xs.bin",
            "smstateen/rvh_test.bin",
            "zacas/zacas-riscv64-xs.bin",
            "Svpbmt/rvh_test.bin",
            "Svnapot/svnapot-test.bin",
            "Zawrs/Zawrs-zawrs.bin"
        ]
        misc_tests = map(lambda x: os.path.join(base_dir, x), workloads)
        return misc_tests
    
    def __get_ci_rvhtest(self, name=None):
        base_dir = "/nfs/home/share/ci-workloads/H-extension-tests"
        workloads = [
            "riscv-hyp-tests/rvh_test.bin",
            "xvisor_wboxtest/checkpoint.gz",
            "pointer-masking-test/M_HS_test/rvh_test.bin",
            "pointer-masking-test/U_test/hint_UMode_hupmm2/rvh_test.bin",
            "pointer-masking-test/U_test/vu_senvcfgpmm2/rvh_test.bin"
        ]
        rvh_tests = map(lambda x: os.path.join(base_dir, x), workloads)
        return rvh_tests

    def __get_ci_rvvbench(self, name=None):
        base_dir = "/nfs/home/share/ci-workloads"
        workloads = [
            "rvv-bench/poly1305.bin",
            "rvv-bench/mergelines.bin"
        ]
        rvvbench = map(lambda x: os.path.join(base_dir, x), workloads)
        return rvvbench

    def __get_ci_rvvtest(self, name=None):
        base_dir = "/nfs/home/share/ci-workloads/V-extension-tests"
        workloads = [
            "rvv-test/vluxei32.v-0.bin",
            "rvv-test/vlsseg4e32.v-0.bin",
            "rvv-test/vlseg4e32.v-0.bin",
            "rvv-test/vsetvl-0.bin",
            "rvv-test/vsetivli-0.bin",
            "rvv-test/vsuxei32.v-0.bin",
            "rvv-test/vse16.v-0.bin",
            "rvv-test/vsse16.v-1.bin",
            "rvv-test/vlse32.v-0.bin",
            "rvv-test/vsetvli-0.bin",
            "rvv-test/vle16.v-0.bin",
            "rvv-test/vle32.v-0.bin",
            "rvv-test/vfsgnj.vv-0.bin",
            "rvv-test/vfadd.vf-0.bin",
            "rvv-test/vfsub.vf-0.bin",
            "rvv-test/vslide1down.vx-0.bin"
        ]
        rvv_test = map(lambda x: os.path.join(base_dir, x), workloads)
        return rvv_test

    def __get_ci_F16test(self, name=None):
        base_dir = "/nfs/home/share/ci-workloads/vector/F16-tests/build"
        workloads = [
            "rv64uzfhmin-p-fzfhmincvt.bin",
            "rv64uzfh-p-fadd.bin",
            "rv64uzfh-p-fclass.bin",
            "rv64uzfh-p-fcmp.bin",
            "rv64uzfh-p-fcvt.bin",
            "rv64uzfh-p-fcvt_w.bin",
            "rv64uzfh-p-fdiv.bin",
            "rv64uzfh-p-fmadd.bin",
            "rv64uzfh-p-fmin.bin",
            "rv64uzfh-p-ldst.bin",
            "rv64uzfh-p-move.bin",
            "rv64uzfh-p-recoding.bin",
            "rv64uzvfh-p-vfadd.bin",
            "rv64uzvfh-p-vfclass.bin",
            "rv64uzvfh-p-vfcvtfx.bin",
            "rv64uzvfh-p-vfcvtfxu.bin",
            "rv64uzvfh-p-vfcvtrxf.bin",
            "rv64uzvfh-p-vfcvtrxuf.bin",
            "rv64uzvfh-p-vfcvtxf.bin",
            "rv64uzvfh-p-vfcvtxuf.bin",
            "rv64uzvfh-p-vfdiv.bin",
            "rv64uzvfh-p-vfdown.bin",
            "rv64uzvfh-p-vfmacc.bin",
            "rv64uzvfh-p-vfmadd.bin",
            "rv64uzvfh-p-vfmax.bin",
            "rv64uzvfh-p-vfmerge.bin",
            "rv64uzvfh-p-vfmin.bin",
            "rv64uzvfh-p-vfmsac.bin",
            "rv64uzvfh-p-vfmsub.bin",
            "rv64uzvfh-p-vfmul.bin",
            "rv64uzvfh-p-vfmv.bin",
            "rv64uzvfh-p-vfncvtff.bin",
            "rv64uzvfh-p-vfncvtfx.bin",
            "rv64uzvfh-p-vfncvtfxu.bin",
            "rv64uzvfh-p-vfncvtrff.bin",
            "rv64uzvfh-p-vfncvtrxf.bin",
            "rv64uzvfh-p-vfncvtrxuf.bin",
            "rv64uzvfh-p-vfncvtxf.bin",
            "rv64uzvfh-p-vfncvtxuf.bin",
            "rv64uzvfh-p-vfnmacc.bin",
            "rv64uzvfh-p-vfnmadd.bin",
            "rv64uzvfh-p-vfnmsac.bin",
            "rv64uzvfh-p-vfnmsub.bin",
            "rv64uzvfh-p-vfrdiv.bin",
            "rv64uzvfh-p-vfrec7.bin",
            "rv64uzvfh-p-vfredmax.bin",
            "rv64uzvfh-p-vfredmin.bin",
            "rv64uzvfh-p-vfredosum.bin",
            "rv64uzvfh-p-vfredusum.bin",
            "rv64uzvfh-p-vfrsqrt7.bin",
            "rv64uzvfh-p-vfrsub.bin",
            "rv64uzvfh-p-vfsgnj.bin",
            "rv64uzvfh-p-vfsgnjn.bin",
            "rv64uzvfh-p-vfsgnjx.bin",
            "rv64uzvfh-p-vfsqrt.bin",
            "rv64uzvfh-p-vfsub.bin",
            "rv64uzvfh-p-vfup.bin",
            "rv64uzvfh-p-vfwadd.bin",
            "rv64uzvfh-p-vfwadd-w.bin",
            "rv64uzvfh-p-vfwcvtff.bin",
            "rv64uzvfh-p-vfwcvtfx.bin",
            "rv64uzvfh-p-vfwcvtfxu.bin",
            "rv64uzvfh-p-vfwcvtrxf.bin",
            "rv64uzvfh-p-vfwcvtrxuf.bin",
            "rv64uzvfh-p-vfwcvtxf.bin",
            "rv64uzvfh-p-vfwcvtxuf.bin",
            "rv64uzvfh-p-vfwmacc.bin",
            "rv64uzvfh-p-vfwmsac.bin",
            "rv64uzvfh-p-vfwmul.bin",
            "rv64uzvfh-p-vfwnmacc.bin",
            "rv64uzvfh-p-vfwnmsac.bin",
            "rv64uzvfh-p-vfwredosum.bin",
            "rv64uzvfh-p-vfwredusum.bin",
            "rv64uzvfh-p-vfwsub.bin",
            "rv64uzvfh-p-vfwsub-w.bin",
            "rv64uzvfh-p-vmfeq.bin",
            "rv64uzvfh-p-vmfge.bin",
            "rv64uzvfh-p-vmfgt.bin",
            "rv64uzvfh-p-vmfle.bin",
            "rv64uzvfh-p-vmflt.bin",
            "rv64uzvfh-p-vmfne.bin"
        ]
        f16_test = map(lambda x: os.path.join(base_dir, x), workloads)
        return f16_test
    def __get_ci_zcbtest(self, name=None):
        base_dir = "/nfs/home/share/ci-workloads/zcb-test"
        workloads = [
            "zcb-test-riscv64-xs.bin"
        ]
        zcb_test = map(lambda x: os.path.join(base_dir, x), workloads)
        return zcb_test

    def __get_ci_mc(self, name=None):
        base_dir = "/nfs/home/share/ci-workloads"
        workloads = [
            "nexus-am-workloads/tests/dualcoretest/ldvio-riscv64-xs.bin"
        ]
        mc_tests = map(lambda x: os.path.join(base_dir, x), workloads)
        return mc_tests

    def __get_ci_nodiff(self, name=None):
        base_dir = "/nfs/home/share/ci-workloads"
        workloads = [
            "cache-management/cacheoptest-riscv64-xs.bin"
        ]
        tests = map(lambda x: os.path.join(base_dir, x), workloads)
        return tests

    def __am_apps_path(self, bench):
        base_dir = '/nfs/home/share/ci-workloads/nexus-am-workloads/apps'
        filename = f"{bench}-riscv64-xs.bin"
        return [os.path.join(base_dir, bench, filename)]

    def __get_ci_workloads(self, name):
        workloads = {
            "linux-hello": "bbl.bin",
            "linux-hello-smp": "bbl.bin",
            "linux-hello-opensbi": "fw_payload.bin",
            "linux-hello-smp-opensbi": "fw_payload.bin",
            "linux-hello-new": "bbl.bin",
            "linux-hello-smp-new": "bbl.bin",
            "povray": "_700480000000_.gz",
            "mcf": "_17520000000_.gz",
            "xalancbmk": "_266100000000_.gz",
            "gcc": "_39720000000_.gz",
            "namd": "_434640000000_.gz",
            "milc": "_103620000000_.gz",
            "lbm": "_140840000000_.gz",
            "gromacs": "_275480000000_.gz",
            "wrf": "_1916220000000_.gz",
            "astar": "_122060000000_.gz",
            "hmmer-Vector": "_6598_0.250135_.zstd"
        }
        if name in workloads:
            return [os.path.join("/nfs/home/share/ci-workloads", name, workloads[name])]
        # select a random SPEC checkpoint
        assert(name == "random")
        all_cpt_dir = [
            "/nfs/home/share/checkpoints_profiles/spec06_rv64gcb_o2_20m/take_cpt",
            "/nfs/home/share/checkpoints_profiles/spec06_rv64gcb_o3_20m/take_cpt",
            "/nfs/home/share/checkpoints_profiles/spec06_rv64gc_o2_20m/take_cpt",
            "/nfs/home/share/checkpoints_profiles/spec06_rv64gc_o2_50m/take_cpt",
            "/nfs/home/share/checkpoints_profiles/spec17_rv64gcb_o2_20m/take_cpt",
            "/nfs/home/share/checkpoints_profiles/spec17_rv64gcb_o3_20m/take_cpt",
            "/nfs/home/share/checkpoints_profiles/spec17_rv64gc_o2_50m/take_cpt",
            "/nfs/home/share/checkpoints_profiles/spec17_speed_rv64gcb_o3_20m/take_cpt",
            "/nfs/home/share/checkpoints_profiles/spec06_rv64gcb_O3_20m_gcc12.2.0-intFpcOff-jeMalloc/zstd-checkpoint-0-0-0",
            "/nfs/home/share/checkpoints_profiles/spec06_gcc15_rv64gcbv_O3_lto_base_nemu_single_core_NEMU_archgroup_2024-10-12-16-05/checkpoint-0-0-0"
        ]
        all_gcpt = load_all_gcpt(all_cpt_dir)
        return [random.choice(all_gcpt)]

    def run_ci(self, test):
        all_tests = {
            "cputest": self.__get_ci_cputest,
            "riscv-tests": self.__get_ci_rvtest,
            "misc-tests": self.__get_ci_misc,
            "mc-tests": self.__get_ci_mc,
            "nodiff-tests": self.__get_ci_nodiff,
            "rvh-tests": self.__get_ci_rvhtest,
            "microbench": self.__am_apps_path,
            "coremark": self.__am_apps_path,
            "coremark-1-iteration": self.__am_apps_path,
            "rvv-bench": self.__get_ci_rvvbench,
            "rvv-test": self.__get_ci_rvvtest,
            "f16_test": self.__get_ci_F16test,
            "zcb-test": self.__get_ci_zcbtest
        }
        for target in all_tests.get(test, self.__get_ci_workloads)(test):
            print(target)
            ret = self.run_emu(target)
            if ret:
                if self.args.default_wave_home != self.args.wave_home:
                    print("copy wave file to " + self.args.wave_home)
                    self.__exec_cmd(f"cp $NOOP_HOME/build/*.vcd $WAVE_HOME")
                    self.__exec_cmd(f"cp $NOOP_HOME/build/*.fst $WAVE_HOME")
                    self.__exec_cmd(f"cp $NOOP_HOME/build/emu $WAVE_HOME")
                    self.__exec_cmd(f"cp $NOOP_HOME/build/rtl/SimTop.v $WAVE_HOME")
                    self.__exec_cmd(f"cp $NOOP_HOME/build/*.db $WAVE_HOME")
                return ret
        return 0

    def run_ci_vcs(self, test):
        all_tests = {
            "cputest": self.__get_ci_cputest,
            "riscv-tests": self.__get_ci_rvtest,
            "misc-tests": self.__get_ci_misc,
            "mc-tests": self.__get_ci_mc,
            "nodiff-tests": self.__get_ci_nodiff,
            "rvh-tests": self.__get_ci_rvhtest,
            "microbench": self.__am_apps_path,
            "coremark": self.__am_apps_path,
            "coremark-1-iteration": self.__am_apps_path,
            "rvv-bench": self.__get_ci_rvvbench,
            "rvv-test": self.__get_ci_rvvtest,
            "f16_test": self.__get_ci_F16test,
            "zcb-test": self.__get_ci_zcbtest
        }
        for target in all_tests.get(test, self.__get_ci_workloads)(test):
            print(target)
            ret = self.run_simv(target)
            if ret:
                if self.args.default_wave_home != self.args.wave_home:
                    print("copy wave file to " + self.args.wave_home)
                    self.__exec_cmd(f"cp $NOOP_HOME/build/*.fsdb $WAVE_HOME")
                    self.__exec_cmd(f"cp $NOOP_HOME/build/simv $WAVE_HOME")
                    self.__exec_cmd(f"cp $NOOP_HOME/build/rtl/SimTop.v $WAVE_HOME")
                    self.__exec_cmd(f"cp $NOOP_HOME/build/*.db $WAVE_HOME")
                return ret
        return 0

def get_free_cores(n):
    numa_re = re.compile(r'.*numactl +.*-C +([0-9]+)-([0-9]+).*')
    while True:
        disable_cores = []
        for proc in psutil.process_iter():
            try:
                joint = ' '.join(proc.cmdline())
                numa_match = numa_re.match(joint)
                if numa_match and 'ssh' not in proc.name():
                    disable_cores.extend(range(int(numa_match.group(1)), int(numa_match.group(2)) + 1))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        num_logical_core = psutil.cpu_count(logical=False)
        core_usage = psutil.cpu_percent(interval=1, percpu=True)
        num_window = num_logical_core // n
        for i in range(num_window):
            if set(disable_cores) & set(range(i * n, i * n + n)):
                continue
            window_usage = core_usage[i * n : i * n + n]
            if sum(window_usage) < 30 * n and True not in map(lambda x: x > 90, window_usage):
                return (((i * n) % num_logical_core) // (num_logical_core // 2), i * n, i * n + n - 1)
        print(f"No free {n} cores found. CPU usage: {core_usage}\n")
        time.sleep(random.uniform(1, 60))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Python wrapper for XiangShan')
    parser.add_argument('workload', nargs='?', type=str, default="",
                        help='input workload file in binary format')
    # actions
    parser.add_argument('--build', action='store_true', help='build XS emu')
    parser.add_argument('--generate', action='store_true', help='generate XS verilog')
    parser.add_argument('--vcs-gen', action='store_true', help='generate XS sim verilog for vcs')
    parser.add_argument('--vcs-build', action='store_true', help='build XS simv')
    parser.add_argument('--ci', nargs='?', type=str, const="", help='run CI tests')
    parser.add_argument('--ci-vcs', nargs='?', type=str, const="", help='run CI tests on simv')
    parser.add_argument('--clean', action='store_true', help='clean up XiangShan CI workspace')
    parser.add_argument('--timeout', nargs='?', type=int, default=None, help='timeout (in seconds)')
    # environment variables
    parser.add_argument('--nemu', nargs='?', type=str, help='path to nemu')
    parser.add_argument('--am', nargs='?', type=str, help='path to nexus-am')
    parser.add_argument('--dramsim3', nargs='?', type=str, help='path to dramsim3')
    parser.add_argument('--rvtest', nargs='?', type=str, help='path to riscv-tests')
    parser.add_argument('--wave-dump', nargs='?', type=str , help='path to dump wave')
    # chisel arguments
    parser.add_argument('--enable-log', action='store_true', help='enable log')
    parser.add_argument('--num-cores', type=int, help='number of cores')
    # makefile arguments
    parser.add_argument('--release', action='store_true', help='enable release')
    parser.add_argument('--spike', action='store_true', help='enable spike diff')
    parser.add_argument('--with-dramsim3', action='store_true', help='enable dramsim3')
    parser.add_argument('--threads', nargs='?', type=int, help='number of emu threads')
    parser.add_argument('--make-threads', nargs='?', type=int, help='number of make threads', default=200)
    parser.add_argument('--trace', action='store_true', help='enable vcd waveform')
    parser.add_argument('--trace-fst', action='store_true', help='enable fst waveform')
    parser.add_argument('--config', nargs='?', type=str, help='config')
    parser.add_argument('--yaml-config', nargs='?', type=str, help='yaml config')
    parser.add_argument('--emu-optimize', nargs='?', type=str, help='verilator optimization letter')
    parser.add_argument('--xprop', action='store_true', help='enable xprop for vcs')
    parser.add_argument('--issue', nargs='?', type=str, help='CHI issue')
    # emu arguments
    parser.add_argument('--numa', action='store_true', help='use numactl')
    parser.add_argument('--diff', nargs='?', default="./ready-to-run/riscv64-nemu-interpreter-so", type=str, help='nemu so')
    parser.add_argument('--max-instr', nargs='?', type=int, help='max instr')
    parser.add_argument('--disable-fork', action='store_true', help='disable lightSSS')
    parser.add_argument('--no-diff', action='store_true', help='disable difftest')
    parser.add_argument('--ram-size', nargs='?', type=str, help='manually set simulation memory size (8GB by default)')
    parser.add_argument('--gcpt-restore-bin', type=str, default="", help="specify the bin used to restore from gcpt")
    # both makefile and emu arguments
    parser.add_argument('--no-db', action='store_true', help='disable chiseldb dump')
    parser.add_argument('--pgo', nargs='?', type=str, help='workload for pgo (null to disable pgo)')
    parser.add_argument('--pgo-max-cycle', nargs='?', default=400000, type=int, help='maximun cycle to train pgo')
    parser.add_argument('--pgo-emu-args', nargs='?', default='--no-diff', type=str, help='emu arguments for pgo')
    parser.add_argument('--llvm-profdata', nargs='?', type=str, help='corresponding llvm-profdata command of clang to compile emu, do not set with GCC')

    args = parser.parse_args()

    xs = XiangShan(args)
    ret = xs.run(args)

    sys.exit(ret)
