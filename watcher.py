#!/usr/bin/env python3

import argparse
import datetime
import difflib
import importlib.util
import json
import os
import re
import time
import typing
import logging
import hashlib

from enum import Enum
from bs4 import BeautifulSoup

import selenium.webdriver.remote.webdriver
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox import service
from selenium.webdriver.firefox.options import Options
from selenium.common.exceptions import NoSuchElementException

spec = importlib.util.spec_from_file_location("notifier.py", "/usr/local/bin/notifier.py")
notifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notifier)

CONFIG_REQUIRED_PAGE_KEYS = ["name", "url", "css_selector"]
CONFIG_OPTIONAL_PAGE_KEYS = ["receiver", "page_load_wait_time", "remove_regexes"]

print_progress: bool = True
driver: typing.Optional[selenium.webdriver.remote.webdriver.WebDriver] = None


class DiffResult(Enum):
	NO_CHANGE = 1
	INITIALIZED = 2
	CHANGE = 3
	SELECTOR_CHANGED = 4


def send_notification(recipient: str, message: str):
	if not recipient:
		return

	notifier.send_notification("Website Watcher", message, recipient)


def get_relevant_content(url: str, css_selector: str, wait_time: int, geckodriver_path: str):
	global driver

	if driver is None:
		logging.info("  Initializing selenium driver...")
		options = Options()
		options.add_argument("-headless")
		firefox_service = service.Service(executable_path=geckodriver_path, log_path=os.devnull)
		driver = webdriver.Firefox(service=firefox_service, options=options)

	driver.get(url)

	# Wait a bit for selenium to finish
	logging.info(f"  Waiting {wait_time}s for page to load...")
	time.sleep(wait_time)

	# Find relevant content by given css selector
	elem = driver.find_element(By.CSS_SELECTOR, css_selector)

	# Remove all carriage returns as this will cause problems with caching
	source = elem.get_attribute("innerHTML").replace("\r", "")

	return source


def get_file_name_for_url(url: str):
	return hashlib.sha256(url.encode("utf-8")).hexdigest()


def get_file_path_for_url(url: str, base_dir: str):
	filename = get_file_name_for_url(url)
	return os.path.join(base_dir, filename)


def read_cache(url: str, cache_dir: str):
	filepath = get_file_path_for_url(url, cache_dir)

	if not os.path.isfile(filepath):
		return False, False

	with open(filepath, "r") as fh:
		lines = fh.readlines()
		return lines[0].replace("\n", ""), list(map(lambda x: x.replace("\n", ""), lines[1:]))


def write_cache(url: str, css_selector: str, content: typing.List[str], cache_dir: str):
	filepath = get_file_path_for_url(url, cache_dir)

	with open(filepath, "w") as fh:
		fh.write(css_selector + "\n")
		for line in content:
			fh.write(line)
			if not line.endswith("\n"):
				fh.write("\n")


def check_if_url_changed(
	page: dict,
	wait_time: int,
	cache_dir: str,
	diff_dir: str,
	geckodriver_path: str
) -> typing.Tuple[DiffResult, str]:

	nickname = page["name"]
	url = page["url"]
	css_selector = page["css_selector"]
	remove_regexes = page.get("remove_regexes", [])

	logging.info(f"Checking '{nickname}' for changes...")

	# Fetch relevant segement from current state
	new_segment = get_relevant_content(url, css_selector, wait_time, geckodriver_path)

	# Remove everything that matches one of the regexes
	for regex in remove_regexes:
		new_segment = re.sub(regex, "", new_segment)

	# Prettify it for comparison
	new_content_parser = BeautifulSoup(new_segment, "html.parser")
	new_segment = new_content_parser.prettify().split("\n")

	logging.info(f"  Reading cache...")

	# Read the cached data
	cached_css_selector, cached_segment = read_cache(url, cache_dir)

	# Nothing to compare against, report that a new URL was initialized
	if not cached_segment:
		write_cache(url, css_selector, new_segment, cache_dir)
		logging.info(f"New page initialized.")
		return DiffResult.INITIALIZED, get_file_name_for_url(url)

	# If the css_selector changed, we need to re-init everything
	if css_selector != cached_css_selector:
		cache_file_path = get_file_path_for_url(url, cache_dir)
		os.unlink(cache_file_path)
		return DiffResult.SELECTOR_CHANGED, ""

	# Write the new segment to the cache
	write_cache(url, css_selector, new_segment, cache_dir)

	# Diff it
	logging.info(f"  Diffing...")
	diff_file_path = get_file_path_for_url(url, diff_dir) + f"_diff_{int(time.time() * 1000)}.html"
	if cached_segment != new_segment:
		# Create the diff result
		with open(diff_file_path, "w") as diff_file_handle:
			html = difflib.HtmlDiff(tabsize=4, wrapcolumn=80).make_file(cached_segment, new_segment)
			soup = BeautifulSoup(html, "html.parser")
			header = soup.new_tag("h3")
			header.string = f"[{nickname}] Change detected at {datetime.datetime.now()}"
			soup.body.insert(0, header)
			diff_file_handle.write(soup.prettify())

		# Save the old and new diff for potential debugging of false positives
		with open(re.sub(".html$", "_old.html", diff_file_path), "w") as diff_file_handle_old:
			diff_file_handle_old.write(f"{cached_segment}")
		with open(re.sub(".html$", "_new.html", diff_file_path), "w") as diff_file_handle_new:
			diff_file_handle_new.write(f"{new_segment}")

		# Return the diff file name
		logging.info(f"Page changed.")
		return DiffResult.CHANGE, os.path.basename(diff_file_path)
	else:
		logging.info(f"No change detected.")
		return DiffResult.NO_CHANGE, ""


def clear_old_files(base_dir: str, max_age_in_seconds: int):
	for entry in os.listdir(base_dir):
		path = os.path.join(base_dir, entry)
		if os.path.isfile(path) and time.time() - os.path.getmtime(path) > max_age_in_seconds:
			os.unlink(path)


def parse_pages_config(pages_config_path: str):
	def check_key_exists(data: dict, key: str, page_idx: int = -1):
		if key not in data:
			message = f"Missing key '{key}'"

			if page_idx >= 0:
				message += f" in page {page_idx + 1}."
			else:
				message += " at the top level."

			raise ValueError(message)

	def check_unknown_keys(data: dict, allowed_keys: list, page_idx: int = -1):
		diff = [item for item in data.keys() if item not in allowed_keys]
		if len(diff) != 0:
			message = f"Extraneous keys {diff}"

			if page_idx >= 0:
				message += f" in page {page_idx + 1}."
			else:
				message += " at the top level."

			logging.warning(message)

	if not os.path.isfile(pages_config_path):
		raise ValueError(f"Then config file '{pages_config_path}' does not exist.")

	with open(pages_config_path, "r") as fh:
		# Parse JSON and check for consistency
		try:
			pages_conf = json.load(fh)
		except json.JSONDecodeError as jde:
			raise ValueError(f"Failed to read config: {jde}")

		if not isinstance(pages_conf, list):
			raise ValueError("The list of pages is not a list.")

		for idx, page in enumerate(pages_conf):
			# Check if all required page keys exist
			map(lambda key: check_key_exists(page, key, idx), CONFIG_REQUIRED_PAGE_KEYS)

			# Check if there are unknown page keys
			check_unknown_keys(page, CONFIG_REQUIRED_PAGE_KEYS + CONFIG_OPTIONAL_PAGE_KEYS, idx)

			# Check if the regexes are valid
			for regex_idx, regex in enumerate(page.get("remove_regexes", [])):
				try:
					re.compile(regex)
				except (re.error, TypeError):
					raise ValueError(f"Page {idx + 1}, regex {regex_idx + 1} is not a valid regex.")

		return pages_conf


def check_change(
	pages: typing.List[typing.Dict],
	cache_dir: str,
	diff_dir: str,
	geckodriver_path: str,
	diff_url: str = None,
	default_recipient: str = None,
	default_page_load_wait_time: int = 5
):

	for page in pages:
		nickname = page["name"]
		url = page["url"]
		recipient = page.get("recipient", default_recipient)
		css_selector = page["css_selector"]
		wait_time = page.get("page_load_wait_time", default_page_load_wait_time)

		# Maybe has to run twice if the selector changed
		for _ in range(2):
			try:
				result, file_name = check_if_url_changed(page, wait_time, cache_dir, diff_dir, geckodriver_path)
			except ValueError as ve:
				error_message = f"Error occured for [{nickname}]({url}): {ve}"
				logging.error(error_message)
				send_notification(recipient, error_message)
				break
			except NoSuchElementException as nsee:
				error_message = f"Error occured for [{nickname}]({url}): {nsee}"
				logging.error(error_message)
				send_notification(recipient, error_message)
				break

			if result == DiffResult.INITIALIZED:
				message = f"Initialized new URL to watch: [{nickname}]({url})."
				message += f"\nUsed CSS selector: '{css_selector}'."

				if diff_url is not None:
					message += f"\nThe segment that will be scanned for can be checked"
					message += f" for correctness [here]({diff_url}{file_name})."
					message += f"\nFirst line is the css selector and not part of the segment."

				send_notification(recipient, message)
			elif result == DiffResult.CHANGE:
				message = f"URL changed: [{nickname}]({url})."

				if diff_url is not None:
					message += f"\nThe diff result can be found [here]({diff_url}{file_name})."

				send_notification(recipient, message)
			elif result == DiffResult.SELECTOR_CHANGED:
				# Repeat the check for initalization
				continue

			break


def close_driver():
	if driver is not None:
		driver.quit()
		logging.info("Closed selenium driver.")


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("pages", help="A JSON file with website configs to be monitored.")
	parser.add_argument("geckodriver_path", help="The path to the geckodriver to use.")
	parser.add_argument(
		"--cache-dir", "-cd",
		required=True,
		help="The directory to store comparison cache files in."
	)
	parser.add_argument(
		"--diff-dir", "-dd",
		required=True,
		help="The directory to store diff results in."
	)
	parser.add_argument(
		"--diff-url", "-du",
		required=True,
		help=(
			"The URL were the diff results are accessible (if you want that)."
			" For this to work, --cache-dir needs to be served by a webserver."
		)
	)
	parser.add_argument(
		"--default-recipient", "-r",
		default=None,
		help=(
			"The default notification receiver that gets errors regarding the URL file. "
			"Defaults to None (no notifications sent)."
		)
	)
	parser.add_argument(
		"--default-page-load-wait-time", "-t",
		type=int,
		default=5,
		help="The default time to wait for a page to load if not given in a page configuration. Defaults to 5 seconds."
	)
	parser.add_argument(
		"--quiet", "-q",
		action="store_true",
		help="Supress output (except errors and stack traces)."
	)
	parser.add_argument(
		"--max-age",
		type=int,
		default=7 * 24 * 60 * 60,
		help="The maximum age for a file (diff or cache) to be kept around without modification. Defaults to 7 days."
	)
	args = parser.parse_args()

	if args.quiet:
		level = logging.ERROR
	else:
		level = logging.INFO

	logging.basicConfig(level=level, format="[%(asctime)s] %(levelname)8s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

	if args.diff_url is not None:
		if not any(args.diff_url.startswith(proto) for proto in ["http://", "https://"]):
			raise ValueError("--diff-url is not valid (protocol missing or invalid).")

		# Add endpoint path terminator if it is missing
		if not args.diff_url.endswith("/"):
			args.diff_url += "/"

	# Create directories if they do not exist
	for directory in [args.cache_dir, args.diff_dir]:
		if not os.path.isdir(directory):
			os.makedirs(directory)

	try:
		pages_config = parse_pages_config(args.pages)
	except ValueError as err:
		msg = f"Error in config file: {err}"
		send_notification(args.default_receiver, msg)
		logging.error(msg)
		close_driver()
		exit(1)

	start = time.time()
	check_change(
		pages_config,
		args.cache_dir,
		args.diff_dir,
		args.geckodriver_path,
		args.diff_url,
		args.default_recipient,
		args.default_page_load_wait_time
	)
	clear_old_files(args.cache_dir, args.max_age)
	clear_old_files(args.diff_dir, args.max_age)

	close_driver()
