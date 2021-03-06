
import sys

if sys.maxsize <= 2**32:
	raise Exception("64 bit Python is required.")

if sys.hexversion < 0x3060000:
	raise Exception("Python 3.6 or higher is required for this script.")

import os
import traceback
import pkg_resources
from pkg_resources import DistributionNotFound, VersionConflict

try:
	with open('packages.txt') as f:
		pkg_resources.require(f.read().splitlines())
except (DistributionNotFound, VersionConflict) as ex:
	traceback.print_exc()
	print("\nDependencies not met. Run `pip install -r packages.txt` to install missing dependencies.")
	sys.exit(1)

import argparse
import configparser
import datetime
import json
import math
import multiprocessing as mp
import random
import re
import string
import signal
import subprocess
import tempfile
from tempfile import TemporaryDirectory
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from argparse import Namespace
from pathlib import Path
from shutil import copy, copytree
from shutil import get_terminal_size as tsize
from shutil import rmtree
from socket import timeout
from subprocess import call
from zipfile import ZipFile

import psutil
from PIL import Image, ImageChops
from orderedset import OrderedSet

from crop import crop
from ref import ref
from updateLib import update as updateLib
from zoom import zoom, zoomRenderboxes

USER_FOLDER = Path("..", "..").resolve()

def printErase(arg):
	try:
		tsiz = tsize()[0]
		print("\r{}{}\n".format(arg, " " * (tsiz*math.ceil(len(arg)/tsiz)-len(arg) - 1)), end="", flush=True)
	except:
		#raise
		pass


def startGameAndReadGameLogs(results, condition, popenArgs, tmpDir, pidBlacklist, rawTags, args):

	pipeOut, pipeIn = os.pipe()
	p = subprocess.Popen(popenArgs, stdout=pipeIn)

	printingStackTraceback = False
	# TODO: keep printing multiline stuff until new print detected
	prevPrinted = False
	def handleGameLine(line):
		nonlocal prevPrinted
		line = line.rstrip('\n')
		if re.match(r'^\ *\d+(?:\.\d+)? *[^\n]*$', line) is None:
			if prevPrinted:
				printErase(line)
			return

		prevPrinted = False

		m = re.match(r'^\ *\d+(?:\.\d+)? *Script *@__L0laapk3_FactorioMaps__\/data-final-fixes\.lua:\d+: FactorioMaps_Output_RawTagPaths:([^:]+):(.*)$', line, re.IGNORECASE)
		if m is not None:
			rawTags[m.group(1)] = m.group(2)
			if rawTags["__used"]:
				raise Exception("Tags added after they were used.")
		else:
			if printingStackTraceback or line == "stack traceback:":
				printErase("[GAME] %s" % line)
				prevPrinted = True
				return True
			m = re.match(r'^\ *\d+(?:\.\d+)? *Script *@__L0laapk3_FactorioMaps__\/(.*?)(?:(\[info\]) ?(.*))?$', line, re.IGNORECASE)
			if m is not None and m.group(2) is not None:
				printErase(m.group(3))
				prevPrinted = True
			elif m is not None and args.verbose:
				printErase(m.group(1))
				prevPrinted = True
			elif line.lower() in ("error", "warn", "exception", "fail", "invalid") or (args.verbosegame and len(line) > 0):
				printErase("[GAME] %s" % line)
				prevPrinted = True
		return False


	with os.fdopen(pipeOut, 'r') as pipef:

		line = pipef.readline().rstrip("\n")
		printingStackTraceback = handleGameLine(line)
		isSteam = False
		if line.endswith("Initializing Steam API."):
			isSteam = True
		elif not re.match(r'^ *\d+\.\d{3} \d{4}-\d\d-\d\d \d\d:\d\d:\d\d; Factorio (\d+\.\d+\.\d+) \(build (\d+), [^)]+\)$', line):
			raise Exception("Unrecognised output from factorio (maybe your version is outdated?)\n\nOutput from factorio:\n" + line)

		if isSteam:
			# note: possibility to avoid this: https://www.reddit.com/r/Steam/comments/4rgrxj/where_are_launch_options_saved_for_games/
			# requirements for this approach: root?, need to figure out steam userid, parse the file format, ensure no conflicts between instances. overall probably not worth it.
			print("WARNING: Running in limited support mode trough steam. Consider using standalone factorio instead.\n\t If you have any default arguments set in steam for factorio, delete them and restart the script.\n\t Please alt tab to steam and confirm the steam 'start game with arguments' popup.\n\t (Yes, you'll have to click this every time the game starts for the steam version)")
			attrs = ('pid', 'name', 'create_time')

			# on some devices, the previous check wasn't enough apparently, so explicitely wait until the log file is created.
			while not os.path.exists(os.path.join(tmpDir, "factorio-current.log")):
				time.sleep(0.4)

			oldest = None
			pid = None
			while pid is None:
				for proc in psutil.process_iter(attrs=attrs):
					pinfo = proc.as_dict(attrs=attrs)
					if pinfo["name"] == "factorio.exe" and pinfo["pid"] not in pidBlacklist and (pid is None or pinfo["create_time"] < oldest):
						oldest = pinfo["create_time"]
						pid = pinfo["pid"]
				if pid is None:
					time.sleep(1)
			# print(f"PID: {pid}")
		else:
			pid = p.pid

		results.extend((isSteam, pid))
		with condition:
			condition.notify()

		psutil.Process(pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)

		if isSteam:
			pipef.close()
			with Path(tmpDir, "factorio-current.log").open("r") as f:
				while psutil.pid_exists(pid):
					where = f.tell()
					line = f.readline()
					if not line:
						time.sleep(0.4)
						f.seek(where)
					else:
						printingStackTraceback = handleGameLine(line)

		else:
			while True:
				line = pipef.readline()
				printingStackTraceback = handleGameLine(line)


def check_update(reverse_update_test:bool = False):
	try:
		print("checking for updates")
		latestUpdates = json.loads(urllib.request.urlopen('https://cdn.jsdelivr.net/gh/L0laapk3/FactorioMaps@latest/updates.json', timeout=30).read())
		with open("updates.json", "r") as f:
			currentUpdates = json.load(f)
		if reverse_update_test:
			latestUpdates, currentUpdates = currentUpdates, latestUpdates

		updates = []
		majorUpdate = False
		currentVersion = (0, 0, 0)
		for verStr, changes in currentUpdates.items():
			ver = tuple(map(int, verStr.split(".")))
			if currentVersion[0] < ver[0] or (currentVersion[0] == ver[0] and currentVersion[1] < ver[1]):
				currentVersion = ver
		for verStr, changes in latestUpdates.items():
			if verStr not in currentUpdates:
				ver = tuple(map(int, verStr.split(".")))
				updates.append((verStr, changes))
		updates.sort(key = lambda u: u[0])
		if len(updates) > 0:

			padding = max(map(lambda u: len(u[0]), updates))
			changelogLines = []
			for update in updates:
				if isinstance(update[1], str):
					updateText = update[1]
				else:
					updateText = str(("\r\n      " + " "*padding).join(update[1]))
				if updateText[0] == "!":
					majorUpdate = True
					updateText = updateText[1:]
				changelogLines.append("    %s: %s" % (update[0].rjust(padding), updateText))
			print("")
			print("")
			print("================================================================================")
			print("")
			print(("  An " + ("important" if majorUpdate else "incremental") + " update has been found!"))
			print("")
			print("  Here's what changed:")
			for line in changelogLines:
				print(line)
			print("")
			print("")
			print("  Download: https://git.io/factoriomaps")
			if majorUpdate:
				print("")
				print("  You can dismiss this by using --no-update (not recommended)")
			print("")
			print("================================================================================")
			print("")
			print("")
		if majorUpdate or reverse_update_test:
			exit(1)

	except (urllib.error.URLError, timeout) as e:
		print("Failed to check for updates. %s: %s" % (type(e).__name__, e))


def link_dir(src: Path, dest:Path):
	if os.name == 'nt':
		subprocess.check_call(("MKLINK", "/J", src.resolve(), dest.resolve()), stdout=subprocess.DEVNULL, shell=True)
	else:
		os.symlink(dest.resolve(), src.resolve())


def link_custom_mod_folder(modpath: Path):
	print(f"Verifying mod version in custom mod folder ({modpath})")
	modpattern = re.compile(r'^L0laapk3_FactorioMaps_', flags=re.IGNORECASE)
	for entry in [entry for entry in modpath.iterdir() if modpattern.match(entry.name)]:
		print("Found other factoriomaps mod in custom mod folder, deleting.")
		path = Path(modpath, entry)
		if path.is_file() or path.is_symlink():
			path.unlink()
		elif path.is_dir():
			rmtree(path)
		else:
			raise Exception(f"Unable to remove {path} unknown type")

	link_dir(Path(modpath, Path('.').resolve().name), Path("."))


def change_modlist(modpath: Path,newState: bool):
	print(f"{'Enabling' if newState else 'Disabling'} FactorioMaps mod")
	done = False
	modlistPath = Path(modpath, "mod-list.json")
	with modlistPath.open("r") as f:
		modlist = json.load(f)
	for mod in modlist["mods"]:
		if mod["name"] == "L0laapk3_FactorioMaps":
			mod["enabled"] = newState
			done = True
			break
	if not done:
		modlist["mods"].append({"name": "L0laapk3_FactorioMaps", "enabled": newState})
	with modlistPath.open("w") as f:
		json.dump(modlist, f, indent=2)


def build_autorun(args: Namespace, work_folder:Path, out_folder: Path, is_first_snapshot: bool):
	printErase("Building autorun.lua")
	map_info_path = Path(work_folder, "mapInfo.json")
	if map_info_path.is_file():
		with map_info_path.open("r", encoding='utf-8') as f:
			mapInfoLua = re.sub(r'"([^"]+)" *:', lambda m: '["'+m.group(1)+'"] = ', f.read().replace("[", "{").replace("]", "}"))
			# TODO: Update for new argument parsing
#			if is_first_snapshot:
#				f.seek(0)
#				mapInfo = json.load(f)
#				if "options" in mapInfo:
#					for kwarg in changedKwargs:
#						if kwarg in ("hd", "dayonly", "nightonly", "build-range", "connect-range", "tag-range"):
#							printErase("Warning: flag '" + kwarg + "' is overriden by previous setting found in existing timeline.")
	else:
		mapInfoLua = "{}"

	is_first_snapshot = False

	chunk_cache_path = Path(work_folder, "chunkCache.json")
	if chunk_cache_path.is_file():
		with chunk_cache_path.open("r") as f:
			chunkCache = re.sub(r'"([^"]+)" *:', lambda m: '["'+m.group(1)+'"] = ', f.read().replace("[", "{").replace("]", "}"))
	else:
		chunkCache = "{}"

	def lower_bool(value: bool):
		return str(value).lower()

	with open("autorun.lua", "w", encoding="utf-8") as f:
		print(args.surface)
		surfaceString = '{"' + '", "'.join(args.surface) + '"}' if args.surface else "nil"
		autorunString = \
f'''fm.autorun = {{
HD = {lower_bool(args.hd)},
day = {lower_bool(args.day)},
night = {lower_bool(args.night)},
alt_mode = {lower_bool(args.altmode)},
tags = {lower_bool(args.tags)},
around_tag_range = {args.tag_range},
around_build_range = {args.build_range},
around_connect_range = {args.connect_range},
connect_types = {{"lamp", "electric-pole", "radar", "straight-rail", "curved-rail", "rail-signal", "rail-chain-signal", "locomotive", "cargo-wagon", "fluid-wagon", "car"}},
date = "{datetime.datetime.strptime(args.date, "%d/%m/%y").strftime("%d/%m/%y")}",
surfaces = {surfaceString},
name = "{str(out_folder) + "/"}",
mapInfo = {mapInfoLua.encode("utf-8").decode("unicode-escape")},
chunkCache = {chunkCache}
}}'''
		f.write(autorunString)
		if args.verbose:
			printErase(autorunString)


def build_config(args: Namespace, temporary_directory):
	printErase("Building config.ini")
	if args.verbose > 2:
		print(f"Using temporary directory '{temporary_directory}'")
	config_path = Path(temporary_directory, "config","config.ini")
	config_path.parent.mkdir(parents=True)

	config = configparser.ConfigParser()
	config.read(Path(args.config_path, "config.ini"))

	if "interface" not in config:
		config["interface"] = {}
	config["interface"]["show-tips-and-tricks"] = "false"

	if "path" not in config:
		config["path"] = {}
	config["path"]["write-data"] = temporary_directory

	if "graphics" not in config:
		config["graphics"] = {}
	config["graphics"]["screenshots-threads-count"] = str(args.screenshotthreads if args.screenshotthreads else args.maxthreads)
	config["graphics"]["max-threads"] = config["graphics"]["screenshots-threads-count"]

	with config_path.open("w+") as config_file:
		config_file.writelines(("; version=3\n", ))
		config.write(config_file, space_around_delimiters=False)

	link_dir(Path(temporary_directory, "script-output"), Path(USER_FOLDER, "script-output"))
	copy(Path(USER_FOLDER, 'player-data.json'), temporary_directory)

	return config_path


def auto(*args):

	lock = threading.Lock()
	def kill(pid, onlyStall=False):
		if pid:
			with lock:
				if not onlyStall and psutil.pid_exists(pid):

					if os.name == 'nt':
						subprocess.check_call(("taskkill", "/pid", str(pid)), stdout=subprocess.DEVNULL, shell=True)
					else:
						subprocess.check_call(("killall", "factorio"), stdout=subprocess.DEVNULL)	# TODO: kill correct process instead of just killing all

					while psutil.pid_exists(pid):
						time.sleep(0.1)

					printErase("killed factorio")

		#time.sleep(0.1)

	parser = argparse.ArgumentParser(description="FactorioMaps")
	daytime = parser.add_mutually_exclusive_group()
	daytime.add_argument("--dayonly", dest="night", action="store_false", help="Only take daytime screenshots.")
	daytime.add_argument("--nightonly", dest="day", action="store_false", help="Only take nighttime screenshots.")
	parser.add_argument("--hd", action="store_true", help="Take screenshots of resolution 64 x 64 pixels per in-game tile.")
	parser.add_argument("--no-altmode", dest="altmode", action="store_false", help="Hides entity info (alt mode).")
	parser.add_argument("--no-tags", dest="tags", action="store_false", help="Hides map tags")
	parser.add_argument("--build-range", type=float, default=5.2, help="The maximum range from buildings around which pictures are saved (in chunks, 32 by 32 in-game tiles).")
	parser.add_argument("--connect-range", type=float, default=1.2, help="The maximum range from connection buildings (rails, electric poles) around which pictures are saved.")
	parser.add_argument("--tag-range", type=float, default=5.2, help="The maximum range from mapview tags around which pictures are saved.")
	parser.add_argument("--surface", action="append", default=[], help="Used to capture other surfaces. If left empty, the surface the player is standing on will be used. To capture multiple surfaces, use the argument multiple times: --surface nauvis --surface 'Factory floor 1'")
	parser.add_argument("--factorio", type=Path, help="Use factorio.exe from PATH instead of attempting to find it in common locations.")
	parser.add_argument("--modpath", type=lambda p: Path(p).resolve(), default=Path(USER_FOLDER, 'mods'), help="Use PATH as the mod folder.")
	parser.add_argument("--config-path", type=lambda p: Path(p).resolve(), default=Path(USER_FOLDER, 'config'), help="Use PATH as the mod folder.")
	parser.add_argument("--basepath", default="FactorioMaps", help="Output to script-output\\RELPATH instead of script-output\\FactorioMaps. (Factorio cannot output outside of script-output)")
	parser.add_argument("--date", default=datetime.date.today().strftime("%d/%m/%y"), help="Date attached to the snapshot, default is today. [dd/mm/yy]")
	parser.add_argument('--verbose', '-v', action='count', default=0, help="Displays factoriomaps script logs.")
	parser.add_argument('--verbosegame', action='count', default=0, help="Displays all game logs.")
	parser.add_argument("--no-update", "--noupdate", dest="update", action="store_false", help="Skips the update check.")
	parser.add_argument("--reverseupdatetest", action="store_true", help=argparse.SUPPRESS)
	parser.add_argument("--maxthreads", type=int, default=mp.cpu_count(), help="Sets the number of threads used for all steps. By default this is equal to the amount of logical processor cores available.")
	parser.add_argument("--cropthreads", type=int, default=None, help="Sets the number of threads used for the crop step.")
	parser.add_argument("--refthreads", type=int, default=None, help="Sets the number of threads used for the crossreferencing step.")
	parser.add_argument("--zoomthreads", type=int, default=None, help="Sets the number of threads used for the zoom step.")
	parser.add_argument("--screenshotthreads", type=int, default=None, help="Set the number of screenshotting threads factorio uses.")
	parser.add_argument("--delete", action="store_true", help="Deletes the output folder specified before running the script.")
	parser.add_argument("--dry", action="store_true", help="Skips starting factorio, making screenshots and doing the main steps, only execute setting up and finishing of script.")
	parser.add_argument("outfolder", nargs="?", help="Output folder for the generated snapshots.")
	parser.add_argument("savename", nargs="*", help="Names of the savegames to generate snapshots from. If no savegames are provided the latest save or the save matching outfolder will be gerated.")
	parser.add_argument("--force-lib-update", action="store_true", help="Forces an update of the leaflet library.")

	args = parser.parse_args()
	if args.verbose > 0:
		print(args)

	if args.update:
		check_update(args.reverseupdatetest)

	saves = Path(USER_FOLDER, "saves")
	if args.outfolder:
		foldername = args.outfolder
	else:
		timestamp, file_path = max(
			(save.stat().st_mtime, save)
			for save in saves.iterdir()
			if save.stem not in {"_autosave1", "_autosave2", "_autosave3"}
		)
		foldername = file_path.stem
		print("No save name passed. Using most recent save: %s" % foldername)
	savenames = args.savename or [foldername]

	save_games = OrderedSet()
	for save_name in savenames:
		glob_results = list(saves.glob(save_name))
		glob_results += list(saves.glob(f"{save_name}.zip"))

		if not glob_results:
			print(f'Cannot find savefile: "{save_name}"')
			raise ValueError(f'Cannot find savefile: "{save_name}"')
		results = [save for save in glob_results if save.is_file()]
		for result in results:
			save_games.add(result.stem)

	if args.verbose > 0:
		print(f"Will generate snapshots for : {list(save_games)}")

	windowsPaths = [
		"Program Files/Factorio/bin/x64/factorio.exe",
		"Games/Factorio/bin/x64/factorio.exe",
		"Program Files (x86)/Steam/steamapps/common/Factorio/bin/x64/factorio.exe",
		"Steam/steamapps/common/Factorio/bin/x64/factorio.exe",
	]

	available_drives = [
		"%s:/" % d for d in string.ascii_uppercase if Path(f"{d}:/").exists()
	]
	possiblePaths = [
		drive + path for drive in available_drives for path in windowsPaths
	] + ["../../bin/x64/factorio.exe", "../../bin/x64/factorio",]
	try:
		factorioPath = next(
			x
			for x in map(Path, [args.factorio] if args.factorio else possiblePaths)
			if x.is_file()
		)
	except StopIteration:
		raise Exception(
			"Can't find factorio.exe. Please pass --factorio=PATH as an argument."
		)

	print("factorio path: {}".format(factorioPath))

	psutil.Process(os.getpid()).nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 5)

	basepath = Path(USER_FOLDER, "script-output", args.basepath)
	workthread = None

	workfolder = Path(basepath, foldername).resolve()
	try:
		print("output folder: {}".format(workfolder.relative_to(Path(USER_FOLDER))))
	except ValueError:
		print("output folder: {}".format(workfolder.resolve()))

	try:
		workfolder.mkdir(parents=True, exist_ok=True)
	except FileExistsError:
		raise Exception(f"{workfolder} exists and is not a directory!")

	updateLib(args.force_lib_update)

	#TODO: integrity check, if done files aren't there or there are any bmps left, complain.

	if args.modpath.resolve() != Path(USER_FOLDER,"mods").resolve():
		link_custom_mod_folder(args.modpath)

	change_modlist(args.modpath, True)

	manager = mp.Manager()
	rawTags = manager.dict()
	rawTags["__used"] = False

	if args.delete:
		print(f"Deleting output folder ({workfolder})")
		try:
			rmtree(workfolder)
		except (FileNotFoundError, NotADirectoryError):
			pass


	###########################################
	#
	#		Start of Work
	#
	###########################################

	datapath = Path(workfolder, "latest.txt")
	allTmpDirs = []

	is_first_snapshot = True

	try:

		for index, savename in () if args.dry else enumerate(save_games):

			printErase("cleaning up")
			if datapath.is_file():
				datapath.unlink()

			build_autorun(args, workfolder, foldername, is_first_snapshot)
			is_first_snapshot = False

			with TemporaryDirectory(prefix="FactorioMaps-") as temporary_directory:
				config_path = build_config(args, temporary_directory)

				pid = None
				isSteam = None
				pidBlacklist = [p.info["pid"] for p in psutil.process_iter(attrs=['pid', 'name']) if p.info['name'] == "factorio.exe"]
				popenArgs = (
					str(factorioPath),
					'--load-game',
					str(Path(USER_FOLDER, 'saves', savename).absolute()),
					'--disable-audio',
					'--config',
					str(config_path),
					"--mod-directory",
					str(args.modpath.absolute()),
					"--disable-migration-window")
				if args.verbose:
					printErase(popenArgs)

				condition = mp.Condition()
				results = manager.list()

				printErase("starting factorio")
				startLogProcess = mp.Process(
					target=startGameAndReadGameLogs,
					args=(results, condition, popenArgs, temporary_directory, pidBlacklist, rawTags, args)
				)
				startLogProcess.daemon = True
				startLogProcess.start()

				with condition:
					condition.wait()
				isSteam, pid = results[:]

				if isSteam is None:
					raise Exception("isSteam error")
				if pid is None:
					raise Exception("pid error")

				while not datapath.exists():
					time.sleep(0.4)

				open("autorun.lua", 'w').close()

				latest = []
				with datapath.open('r') as f:
					for line in f:
						latest.append(line.rstrip("\n"))
				if args.verbose:
					printErase(latest)

				first_out_folder, timestamp, surface, daytime = latest[-1].split(" ")
				first_out_folder = first_out_folder.replace("/", " ")
				waitfilename = Path(basepath, first_out_folder, "images", timestamp, surface, daytime, "done.txt")

				isKilled = [False]
				def waitKill(isKilled, pid):
					while not isKilled[0]:
						#print(f"Can I kill yet? {os.path.isfile(waitfilename)} {waitfilename}")
						if os.path.isfile(waitfilename):
							isKilled[0] = True
							kill(pid)
							break
						else:
							time.sleep(0.4)

				killThread = threading.Thread(target=waitKill, args=(isKilled, pid))
				killThread.daemon = True
				killThread.start()

				if workthread and workthread.isAlive():
					#print("waiting for workthread")
					workthread.join()

				timestamp = None
				daytimeSurfaces = {}
				for jindex, screenshot in enumerate(latest):
					out_folder, timestamp, surface, daytime = list(map(lambda s: s.replace("|", " "), screenshot.split(" ")))
					out_folder = out_folder.replace("/", " ")
					print(f"Processing {out_folder}/{'/'.join([timestamp, surface, daytime])} ({len(latest) * index + jindex + 1} of {len(latest) * len(save_games)})")

					if daytime in daytimeSurfaces:
						daytimeSurfaces[daytime].append(surface)
					else:
						daytimeSurfaces[daytime] = [surface]

					#print("Cropping %s images" % screenshot)
					print(basepath)
					crop(out_folder, timestamp, surface, daytime, basepath, args)
					waitlocalfilename = os.path.join(basepath, out_folder, "Images", timestamp, surface, daytime, "done.txt")
					if not os.path.exists(waitlocalfilename):
						#print("waiting for done.txt")
						while not os.path.exists(waitlocalfilename):
							time.sleep(0.4)



					def refZoom():
						needsThumbnail = index + 1 == len(save_games)
						#print("Crossreferencing %s images" % screenshot)
						ref(out_folder, timestamp, surface, daytime, basepath, args)
						#print("downsampling %s images" % screenshot)
						zoom(out_folder, timestamp, surface, daytime, basepath, needsThumbnail, args)

						if jindex == len(latest) - 1:
							print("zooming renderboxes", timestamp)
							zoomRenderboxes(daytimeSurfaces, workfolder, timestamp, Path(basepath, first_out_folder, "Images"), args)

					if screenshot != latest[-1]:
						refZoom()
					else:
						startLogProcess.terminate()

						# I have receieved a bug report from feidan in which he describes what seems like that this doesnt kill factorio?

						onlyStall = isKilled[0]
						isKilled[0] = True
						kill(pid, onlyStall)

						if savename == save_games[-1]:
							refZoom()

						else:
							workthread = threading.Thread(target=refZoom)
							workthread.daemon = True
							workthread.start()









		if os.path.isfile(os.path.join(workfolder, "mapInfo.out.json")):
			print("generating mapInfo.json")
			with open(os.path.join(workfolder, "mapInfo.json"), 'r+', encoding='utf-8') as destf, open(os.path.join(workfolder, "mapInfo.out.json"), "r", encoding='utf-8') as srcf:
				data = json.load(destf)
				for mapIndex, mapStuff in json.load(srcf)["maps"].items():
					for surfaceName, surfaceStuff in mapStuff["surfaces"].items():
						if "chunks" in surfaceStuff:
							data["maps"][int(mapIndex)]["surfaces"][surfaceName]["chunks"] = surfaceStuff["chunks"]
						for linkIndex, link in enumerate(surfaceStuff["links"]):
							data["maps"][int(mapIndex)]["surfaces"][surfaceName]["links"][linkIndex]["path"] = link["path"]
							data["maps"][int(mapIndex)]["surfaces"][surfaceName]["links"][linkIndex]["zoom"]["min"] = link["zoom"]["min"]
				destf.seek(0)
				json.dump(data, destf)
				destf.truncate()
			os.remove(os.path.join(workfolder, "mapInfo.out.json"))



		print("updating labels")
		tags = {}
		with open(os.path.join(workfolder, "mapInfo.json"), 'r+', encoding='utf-8') as mapInfoJson:
			data = json.load(mapInfoJson)
			for mapStuff in data["maps"]:
				for surfaceName, surfaceStuff in mapStuff["surfaces"].items():
					if "tags" in surfaceStuff:
						for tag in surfaceStuff["tags"]:
							if "iconType" in tag:
								tags[tag["iconType"] + tag["iconName"][0].upper() + tag["iconName"][1:]] = tag

		rmtree(os.path.join(workfolder, "Images", "labels"), ignore_errors=True)

		modVersions = sorted(
				map(lambda m: (m.group(2).lower(), (m.group(3), m.group(4), m.group(5), m.group(6) is None), m.group(1)),
					filter(lambda m: m,
						map(lambda f: re.search(r"^((.*)_(\d+)\.(\d+)\.(\d+))(\.zip)?$", f, flags=re.IGNORECASE),
							os.listdir(os.path.join(basepath, args.modpath))))),
				key = lambda t: t[1],
				reverse = True)


		rawTags["__used"] = True
		if args.tags:
			for _, tag in tags.items():
				dest = os.path.join(workfolder, tag["iconPath"])
				os.makedirs(os.path.dirname(dest), exist_ok=True)


				rawPath = rawTags[tag["iconType"] + tag["iconName"][0].upper() + tag["iconName"][1:]]


				icons = rawPath.split('|')
				img = None
				for i, path in enumerate(icons):
					m = re.match(r"^__([^\/]+)__[\/\\](.*)$", path)
					if m is None:
						raise Exception("raw path of %s %s: %s not found" % (tag["iconType"], tag["iconName"], path))

					iconColor = m.group(2).split("?")
					icon = iconColor[0]
					if m.group(1) in ("base", "core"):
						src = os.path.join(os.path.split(factorioPath)[0], "../../data", m.group(1), icon + ".png")
					else:
						mod = next(mod for mod in modVersions if mod[0] == m.group(1).lower())
						if not mod[1][3]: #true if mod is zip
							zipPath = os.path.join(basepath, args.modpath, mod[2] + ".zip")
							with ZipFile(zipPath, 'r') as zipObj:
								if len(icons) == 1:
									zipInfo = zipObj.getinfo(os.path.join(mod[2], icon + ".png").replace('\\', '/'))
									zipInfo.filename = os.path.basename(dest)
									zipObj.extract(zipInfo, os.path.dirname(os.path.realpath(dest)))
									src = None
								else:
									src = zipObj.extract(os.path.join(mod[2], icon + ".png").replace('\\', '/'), os.path.join(tempfile.gettempdir(), "FactorioMaps"))
						else:
							src = os.path.join(basepath, args.modpath, mod[2], icon + ".png")

					if len(icons) == 1:
						if src is not None:
							img = Image.open(src)
							w, h = img.size
							img = img.crop((0, 0, h, h))
							img.save(dest)
					else:
						newImg = Image.open(src)
						w, h = newImg.size
						newImg = newImg.crop((0, 0, h, h)).convert("RGBA")
						if len(iconColor) > 1:
							newImg = ImageChops.multiply(newImg, Image.new("RGBA", newImg.size, color=tuple(map(lambda s: int(round(float(s))), iconColor[1].split("%")))))
						if i == 0:
							img = newImg
						else:
							img.paste(newImg.convert("RGB"), (0, 0), newImg)
				if len(icons) > 1:
					img.save(dest)




		#TODO: download leaflet shit

		print("generating mapInfo.js")
		with open(os.path.join(workfolder, "mapInfo.js"), 'w') as outf, open(os.path.join(workfolder, "mapInfo.json"), "r", encoding='utf-8') as inf:
			outf.write('"use strict";\nwindow.mapInfo = JSON.parse(')
			outf.write(json.dumps(inf.read()))
			outf.write(");")


		print("creating index.html")
		copy("web/index.html", os.path.join(workfolder, "index.html"))
		copy("web/index.css", os.path.join(workfolder, "index.css"))
		copy("web/index.js", os.path.join(workfolder, "index.js"))
		try:
			rmtree(os.path.join(workfolder, "lib"))
		except (FileNotFoundError, NotADirectoryError):
			pass
		copytree("web/lib", os.path.join(workfolder, "lib"))



	except KeyboardInterrupt:
		print("keyboardinterrupt")
		kill(pid)
		raise

	finally:

		try:
			kill(pid)
		except:
			pass

		change_modlist(args.modpath, False)

		print("cleaning up")
		for tmpDir in allTmpDirs:
			try:
				os.unlink(os.path.join(tmpDir, "script-output"))
				rmtree(tmpDir)
			except (FileNotFoundError, NotADirectoryError):
				pass

if __name__ == '__main__':
	auto(*sys.argv[1:])
