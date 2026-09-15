#!/usr/bin/env python3

import argparse
import time
import socket
import re
from datetime import datetime
from logger_util import Logger
from ssh_client_mgr import SSHClientWrapper

_SERIAL_LABELS = [r'product', r'(?:chassis|system)', r'board', r'']
_SERIAL_PATTERNS = [
    re.compile(
        rf'^\s*{lbl}\s*serial\s*(?:number|num|no\.?|#)?\s*[:=]\s*(\S+)',
        re.IGNORECASE | re.MULTILINE
    )
    for lbl in _SERIAL_LABELS
]
_PLACEHOLDERS = {"none", "n/a", "na", "unknown", "unspecified", "null", "0"}

# =========================
# TASK DEFINITIONS
# =========================

# Commands for rsmcli
# show versions
RM_VER = "show manager version"
SUP_VER = "show powershelf psu version"
# show FRUs
RM_FRU = "show manager fru"
PSF_FRU = "show powershelf fru"
C13_FRU = "show powershelf c13 fru"
# sensor cmds
TELEMETRY = "show manager telemetry"
HUM = "show manager hsc -b 0 reading"
VOLT = "show manager voltage -b 0 status"
PMR = "show manager powermeter reading"
HLT_PWR = "show manager health --power"
C13_5 = "show powershelf c13 reading -c 5"
C13_READING_ALL = "show powershelf c13 reading" # all c13 modules
# Reading FRU individually
C13_FRU_IND = [f"show powershelf c13 fru -i {x}" for x in range(1,5)]
C13_PW_READING_IND = [f"show powershelf c13 reading -c {x}" for x in range(1, 5)]

# Powershelf commands
C13_STAT = [f"show powershelf c13 status -c {x+1}" for x in range(4)]
C13_STATUS_ALL = "show powershelf c13 status"
C13_READING = [f"show powershelf c13 reading -c {x+1}" for x in range(4)]


# Command List to run ----------
T9_LIST = [RM_FRU, HUM, VOLT, PSF_FRU, C13_FRU, C13_READING_ALL] + C13_FRU_IND


def new_stats(commands):
    return {cmd: {"sent": 0, "failed": 0} for cmd in commands}


def print_w_ts(text):
    print(f"{datetime.now().strftime('%y/%m/%d %H:%M:%S')}\t{text}")
    return

def log_command_summary(logger:Logger, stats):
    """Print + log a per-command table. Returns True if everything passed."""
    width = max([len(c) for c in stats] + [len("COMMAND")])
    header = f"{'COMMAND':<{width}}  {'SENT':>6}  {'FAILED':>6}  {'FAIL %':>8}"
    sep = "-" * len(header)

    lines = [header, sep]
    total_sent = 0
    total_failed = 0
    passed = True

    for cmd, s in stats.items():
        sent, failed = s["sent"], s["failed"]
        pct = (failed / sent * 100) if sent else 0.0
        if pct > 0:
            passed = False
        lines.append(f"{cmd:<{width}}  {sent:>6}  {failed:>6}  {pct:>7.2f}%")
        total_sent += sent
        total_failed += failed

    total_pct = (total_failed / total_sent * 100) if total_sent else 0.0
    lines.append(sep)
    lines.append(f"{'TOTAL':<{width}}  {total_sent:>6}  {total_failed:>6}  {total_pct:>7.2f}%")
    lines.append(sep)
    lines.append(f"RESULT: {'PASSED' if passed else 'FAILED'}")

    table = "\n".join(lines)
    print_w_ts(f"Command execution summary:\n{table}")
    logger.log("SUMMARY", table)
    return passed

def pull_journal_log(node:SSHClientWrapper, node_pos):
    journal_log = Logger("journal_log", node_pos)
    journal_txt = node.query("journalctl -b 0 --output=short-iso-precise --no-pager", 300, False)
    journal_log.log("journalctl", journal_txt)
    journal_log.close()
    return


def extract_serial(fru_output:str):
    """ Pass the FRU and get the serial. Falls back to any field with an usable SN"""
    if not fru_output:
        return None
    for pat in _SERIAL_PATTERNS:
        for m in pat.finditer(fru_output):
            raw = m.group(1).strip()
            if raw.lower() in _PLACEHOLDERS:
                continue
            sn = re.sub(r'[^A-Za-z0-9._-]', '_', raw)[:32]
            if sn.strip('_'):
                return sn
    return None

def wait_for_ssh(host="127.0.0.1", port=22, timeout=300, interval=2, logger=None):
    """
    Wait until SSH port is reachable.
    timeout: total seconds to wait
    interval: seconds between retries
    """
    start = time.time()

    while True:
        try:
            with socket.create_connection((host, port), timeout=5):
                if logger:
                    logger.log("WAIT_FOR_SSH", f"{host}:{port} is reachable")
                return True
        except (socket.timeout, socket.error):
            if time.time() - start > timeout:
                if logger:
                    logger.log("WAIT_FOR_SSH", f"Timeout after {timeout}s")
                return False
            time.sleep(interval)


def check_error_found(output):
    return "Failure" in output

def check_no_reading(sensor_output):
    return"no reading" in sensor_output


def reset_c13_module(rm:SSHClientWrapper, logger:Logger):
    '''
        Reset the c13 module powering it OFF, read the i2c bus
        Power it back on
        Inputs: takes RM object
        Output: Returns FALSE if the bus did not recover
    '''
    print_w_ts("Resetting the C13 module see if that works")
    logger.log("RECOVERY", "Reseting C13")
    rm.query("set powershelf c13 off -c 5")
    rm.query("show powershelf c13 status")
    time.sleep(5)
    rm.query(RM_FRU)
    time.sleep(5)
    rm.query(PSF_FRU)
    time.sleep(5)
    rm.query(C13_FRU)
    time.sleep(5)
    rm.query("set powershelf c13 on -c 5")
    out = rm.query(RM_FRU)
    return not("Failure" in out)

def check_c13_status(rm:SSHClientWrapper):
    s = rm.query(C13_STATUS_ALL)
    print_w_ts(f"Status:\n{s}")
    fru = rm.query(C13_FRU)
    print_w_ts(f"FRU Content:\n{fru}")
    r = rm.query(C13_READING_ALL)
    print_w_ts(f"Reading\n{r}")
    print_w_ts("Completed checking PSHELF Status")
    return

def test_c13_modules(rm:SSHClientWrapper, logger:Logger):

    # Shut down all C13 modules
    for i in range(8):
        print_w_ts(f"Turn off {i+1}")
        rm.query(f"set powershelf c13 off -c {i+1}")
    
    check_c13_status(rm)
    c13_arrangement = [[1,2, 3,4], [5,6, 7,8]]

    for c13 in c13_arrangement:
        logger.log("TEST_MODULE", f"Turning ON {c13}")
        print_w_ts(f"Turning on modules {c13}")
        for module in c13:
            rm.query(f"set powershelf c13 on -c {module}")
        print_w_ts("Waiting for 2 min for C13 to Init...")
        time.sleep(120)
        check_c13_status(rm)
        logger.log("TEST_MODULE", f"Turning OFF modules {c13}")
        for module in c13:
            rm.query(f"set powershelf c13 off -c {module}")
        print_w_ts(f"Turn off modules {c13}")
        check_c13_status(rm)

    return


def find_psu_fw_mismatches(output_str: str, expected_version: str):
    """
    Parse PSU firmware output and return a list of socket numbers whose
    ImageA Version does not match the expected version.

    """
    mismatches = []
    # Match each psu socket block (01:, 02:, ..., 12:)
    pattern = re.compile(
        r'^\s*(\d{2}):\s*\n(.*?)(?=^\s*\d{2}:|\Z)',
        re.MULTILINE | re.DOTALL
    )

    for match in pattern.finditer(output_str):
        socket_num = int(match.group(1))
        block = match.group(2)

        # Ignore PSU not present
        if "Status Description: PSU not present" in block:
            print_w_ts(f"Ignoring Block: {block} since is not present")
            continue
        # Extract ImageA Version
        version_match = re.search(
            r'ImageA Version:\s*([0-9A-Fa-f]+)',
            block
        )
        if version_match:
            actual_version = version_match.group(1)
            if actual_version.upper() != expected_version.upper():
                print_w_ts(f"Found {block} FW mismatch")
                mismatches.append(socket_num)

    return mismatches

# =========================
# TASK EXECUTION
# =========================
def execute_task(commands, logger, port, delay):
    client = SSHClientWrapper(port=port, logger=logger)
    client.connect()

    try:
        while True:
            for cmd in commands:
                client.query(cmd)
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\n[+] Interrupted by user")
    finally:
        client.close()
        logger.close()



def check_rscm(logger:Logger, rm_ip:str, rm_port:int, rm_us:str, rm_pwd:str, iterations:int, check_c13:bool):

    rm = SSHClientWrapper(host=rm_ip, port=rm_port, username=rm_us, password=rm_pwd, logger=logger)
    elapsed = 0
    e_count = 0
    error_latch = False
    stats = new_stats(T9_LIST)
    rm.connect()

    print_w_ts("Display version and FRU")
    v = rm.query(RM_VER)
    fru = rm.query(RM_FRU)
    # Use Powershelf to store the log
    ps = rm.query(SUP_VER)
    pshelf_fru = rm.query(PSF_FRU)
    pshelf_sn = extract_serial(pshelf_fru)
    logger.rename(pshelf_sn)

    print_w_ts(f"System:\n{fru}\nVersion:\n{v}\nPSU Versions:\n{ps}\nPshelf FRU:{pshelf_fru}\n")

    if check_c13:
        test_c13_modules(rm, logger)
        return

    try:
        while elapsed < iterations:
            print_w_ts(f"Checking CYCLE: {elapsed+1}")
            for cmd in T9_LIST:
                try:
                    print_w_ts(f"Checking R-SCM Cli - {elapsed} cmd: {cmd}")
                    stats[cmd]["sent"] += 1 
                    output = rm.query(cmd)
                    if check_error_found(output):
                        stats[cmd]["failed"] += 1          
                        error_latch = True
                        print_w_ts("[----] Error found in command, RM failure")
                        logger.log("error", f"Error found in command: {cmd}")
                        e_count = e_count+1
                        if e_count > 10:
                            logger.log("error", f"10 or more consecutive errors found")
                            print_w_ts("Consecutive error count reached, attempt a reset in c13 c5 module")
                            if reset_c13_module(rm, logger):
                                print_w_ts("recovery successful")
                            else:
                                print_w_ts('Recovery unsuccessful, bus still having trouble')
                                raise KeyboardInterrupt
                    else:
                        time.sleep(0.2)
                        e_count = 0
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    stats[cmd]["failed"] += 1 
                    logger.log("ERROR", f"Exception created, details:\n{e}\n")
                    raise e
                time.sleep(0.2)
            elapsed = elapsed+1
        print_w_ts("[++] Completed all commands in main task without interruptions")
        if error_latch:
            print_w_ts("[FFF] - Test result is fail, one or more systems failed")
        else:
            print_w_ts("[+] No Failures found, screening PASS")

    except KeyboardInterrupt:
        print_w_ts("[+] Interrupted task... exiting now")
    finally:
        passed = log_command_summary(logger, stats) 
        rm.close()
        logger.close()

    return passed


def rscm_psu_fw_upgrade(logger, rm_ip, rm_port, rm_pwd, expected_ver):

    timeout = 100
    rm = SSHClientWrapper(host=rm_ip, port=rm_port, password=rm_pwd, logger=logger)
    rm.connect()
    psu_status = rm.query(SUP_VER)
    upgradeable_psu = find_psu_fw_mismatches(psu_status, expected_ver)
    
    # Upgrade one by one the upgradeable PSUs
    
    for psu in upgradeable_psu:
        # Report current version
        print_w_ts(rm.query(f"show powershelf psu version -s {psu}"))
        print_w_ts("f[++] Will try to upgrade now PSU: {psu}")
        r= rm.query(f"set powershelf psu update -s {psu} -f Flex_M1279207-001_P4020_V000F0E00.hex")
        # check if cmd went ok
        if "PSU firmware update started" in r:
            for i in range(timeout):
                status = rm.query(f"show powershelf psu update -s {psu}")
                print_w_ts(f"PSU update status:\n{status}\n")
                if "Update completed" in status:
                    print_w_ts("Done, moving to next")
                    break
                else:
                    print_w_ts("Waiting 60 sec")
                    time.sleep(60)

    print_w_ts("All PSU upgraded")
    psu_status = rm.query(SUP_VER)
    print_w_ts(psu_status)

    return


# =========================
# MAIN
# =========================
def main():
    parser = argparse.ArgumentParser(description="SSH Task Runner")
    parser.add_argument("-t", type=int, choices=[1, 2], required=True, default=1, help="Task number to be Executed")
    parser.add_argument("-rmip", type=str, default="127.0.0.1", help="Rack Manager IP")
    parser.add_argument("-rpo", type=int, default=22, help="Rack manager SSH Port (default 22)")
    parser.add_argument("-rpw", type=str, default="", help="Sys Contra")
    parser.add_argument("-rus", type=str, default="root", help="Sys log")
    parser.add_argument("-n", type=str, default="10", help="Slot position in a Rack")
    parser.add_argument("-c", type=int, default=1.0, help="Cycle Iteration")
    parser.add_argument("-a", action="store_true")

    args = parser.parse_args()

    task_id = args.t
    node = args.n
    single_check = args.a
    rm_ip = args.rmip # rack manager IP
    rm_port = args.rpo # rack manager's SSH port
    rm_pwd = args.rpw # rm pwd
    rm_us = args.rus # login
    iterations = args.c # number of times to execute

    logger = Logger(task_id, node)

    if task_id == 1:
        check_rscm(logger, rm_ip, rm_port, rm_us, rm_pwd, iterations, single_check)
    elif task_id==2:
        rscm_psu_fw_upgrade(logger, rm_ip, rm_port, node)


if __name__ == "__main__":
    main()