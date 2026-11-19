#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

import argparse
import fcntl
import hashlib
import logging
import os
from pathlib import Path

import mwparserfromhell as mw
from mwparserfromhell.nodes import Heading, Template
import requests


SOURCE = "User talk:Larawag"
ARCHIVE = "User:Larawag/Archive"
API = "https://en.wikipedia.org/w/api.php"

EXPECTED_USERNAME = "LarawagArchiveBot@Archiver"
EXPECTED_ACCOUNT = "LarawagArchiveBot"
MAX_ARCHIVE_BYTES = 500_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

LOG = logging.getLogger("archiver")


def template_title(name):
    """Convert a template name into its normal page title."""
    name = str(name).strip().replace("_", " ")

    if name.lower().startswith("template:"):
        name = name.split(":", 1)[1].strip()

    if not name or ":" in name:
        return None

    if any(character in name for character in "{}[]<>|#\n"):
        return None

    return "Template:" + name[0].upper() + name[1:]


def split_topics(text):
    """Split complete level-2 discussions without changing their text."""
    lead = ""
    topics = []
    current = []
    started = False

    for node in mw.parse(text).nodes:
        if isinstance(node, Heading):
            if node.level == 1:
                raise RuntimeError(
                    "Level-1 heading found. Review page structure."
                )

            if node.level == 2:
                if started:
                    topics.append("".join(current))
                else:
                    lead = "".join(current)

                current = []
                started = True

        current.append(str(node))

    if started:
        topics.append("".join(current))
    else:
        lead = "".join(current)

    if lead + "".join(topics) != text:
        raise RuntimeError("Section reconstruction failed.")

    return lead, topics


def command_nodes(text):
    offset = 0

    for node in mw.parse(text).nodes:
        raw = str(node)
        end = offset + len(raw)

        if isinstance(node, Template) and not node.params:
            start = text.rfind("\n", 0, offset) + 1
            stop = text.find("\n", end)
            after = text[end:stop] if stop >= 0 else text[end:]
            before = text[start:offset]

            if not before.replace(":", "").strip() and not after.strip():
                yield offset, end, template_title(node.name)

        offset = end


def discussion_notice(closed):
    if closed:
        icon = "OOjs UI icon lock.svg"
        title = "This conversation has been closed."
        detail = (
            "Please do not add to it. "
            "Start a new section for further discussion."
        )
    else:
        icon = "Replacement filing cabinet.svg"
        title = "This conversation has been archived."
        detail = (
            "Please start a new section at "
            "[[User talk:Larawag]] for further discussion."
        )

    return (
        '<div style="margin:10px 0;padding:12px 16px;'
        'border:1px solid #a2a9b1;border-radius:12px;'
        'background:var(--background-color-neutral-subtle,#f8f9fa);">\n'
        "[[File:" + icon + "|22px|alt=Discussion status]] "
        "'''" + title + "''' " + detail + "\n</div>"
    )


def format_topic(topic, archived=False):
    for start, end, name in reversed(list(command_nodes(topic))):
        if name == "Template:Closed":
            replacement = (
                "<!-- This conversation has been closed. "
                "Please do not add to it. -->\n"
                + discussion_notice(True)
            )
        elif archived and name == "Template:Archive":
            replacement = discussion_notice(False)
        else:
            continue

        topic = topic[:start] + replacement + topic[end:]

    return topic


def template_calls(text):
    return {
        name
        for _, _, name in command_nodes(text)
        if name
    }


def marker(topic):
    """Create a stable identifier for an exact discussion copy."""
    digest = hashlib.sha256(
        (SOURCE + "\n" + topic).encode("utf-8")
    ).hexdigest()

    return "<!-- LarawagArchiver:" + digest + " -->"


def record(topic):
    return (
        marker(topic)
        + "\n"
        + format_topic(topic, archived=True)
        + "\n<!-- /LarawagArchiver -->"
    )


class Wiki:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "LarawagPersonalArchiver/1.1 "
            "(https://en.wikipedia.org/wiki/User:Larawag)"
        )
        self.account = None

    def request(self, write=False, **params):
        params.update(
            format="json",
            formatversion=2,
            maxlag=5,
        )

        if write:
            response = self.session.post(
                API,
                data=params,
                timeout=60,
            )
        else:
            response = self.session.get(
                API,
                params=params,
                timeout=60,
            )

        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise RuntimeError(
                "Wikipedia API: " + data["error"]["code"]
            )

        return data

    def login(self):
        username = os.environ.get("WIKI_BOT_USERNAME", "")
        password = os.environ.get("WIKI_BOT_PASSWORD", "")

        if username != EXPECTED_USERNAME or not password:
            raise RuntimeError(
                "Missing or unexpected bot credentials."
            )

        token = self.request(
            action="query",
            meta="tokens",
            type="login",
        )["query"]["tokens"]["logintoken"]

        result = self.request(
            True,
            action="login",
            lgname=username,
            lgpassword=password,
            lgtoken=token,
        )

        if result.get("login", {}).get("result") != "Success":
            raise RuntimeError("Bot login failed.")

        actual = self.request(
            action="query",
            meta="userinfo",
        )["query"]["userinfo"]["name"]

        if actual != EXPECTED_ACCOUNT:
            raise RuntimeError(
                "Unexpected authenticated account."
            )

        self.account = actual
        LOG.info("Login confirmed: %s", actual)

    def page(self, title):
        result = self.request(
            action="query",
            prop="revisions",
            titles=title,
            rvprop="ids|content",
            rvslots="main",
        )

        page = result["query"]["pages"][0]

        if "missing" in page:
            return "", 0

        revisions = page.get("revisions")

        if not revisions:
            raise RuntimeError(
                "Could not read page revisions: " + title
            )

        revision = revisions[0]

        return (
            revision["slots"]["main"]["content"],
            revision["revid"],
        )

    def closed_aliases(self, names):
        return set(names) & {"Template:Archive"}

    def save(self, title, text, revision, summary):
        if title not in (SOURCE, ARCHIVE):
            raise RuntimeError(
                "Write outside allowed pages refused."
            )

        if self.account != EXPECTED_ACCOUNT:
            raise RuntimeError(
                "Authenticated bot account required."
            )

        assertions = {
            "assert": "user",
            "assertuser": self.account,
        }

        token = self.request(
            action="query",
            meta="tokens",
            **assertions,
        )["query"]["tokens"]["csrftoken"]

        if revision:
            guard = {
                "baserevid": revision,
                "nocreate": 1,
            }
        else:
            guard = {
                "createonly": 1,
            }

        result = self.request(
            True,
            action="edit",
            title=title,
            text=text,
            summary=summary,
            token=token,
            **assertions,
            **guard,
        )

        if result.get("edit", {}).get("result") != "Success":
            raise RuntimeError("Save was not confirmed.")


def archive(wiki, live):
    source, revision = wiki.page(SOURCE)

    if not revision:
        raise RuntimeError(
            "Source talk page does not exist."
        )

    lead, topics = split_topics(source)
    calls = [template_calls(topic) for topic in topics]

    names = set()

    for group in calls:
        names.update(group)

    aliases = wiki.closed_aliases(names)

    selected = [
        topic
        for topic, group in zip(topics, calls)
        if group & aliases
    ]

    LOG.info(
        "%s: %d eligible discussion(s).",
        "LIVE" if live else "PREVIEW",
        len(selected),
    )

    for topic in selected:
        heading = mw.parse(topic).filter_headings()[0]
        LOG.info("Topic: %s", str(heading.title).strip())

    closed_count = sum(
        format_topic(topic) != topic for topic in topics
    )

    LOG.info(
        "%d discussion(s) need closed notices.",
        closed_count,
    )

    if not live:
        return

    if not selected:
        updated_source = lead + "".join(
            format_topic(topic) for topic in topics
        )

        if updated_source != source:
            wiki.save(
                SOURCE,
                updated_source,
                revision,
                "Add marked discussion notices; keep discussions in place",
            )

        return

    old_archive, archive_revision = wiki.page(ARCHIVE)
    additions = []

    for topic in selected:
        item = record(topic)

        if marker(topic) in old_archive and item not in old_archive:
            raise RuntimeError(
                "Previous archive copy differs. "
                "Manual review needed."
            )

        if item not in old_archive and item not in additions:
            additions.append(item)

    if additions:
        if not archive_revision:
            old_archive = "{{Talk archive|User talk:Larawag}}\n\n"

        updated = (
            old_archive.rstrip("\n")
            + "\n\n"
            + "\n\n".join(additions)
            + "\n"
        )

        if len(updated.encode("utf-8")) > MAX_ARCHIVE_BYTES:
            raise RuntimeError(
                "Archive reached 500 KB. "
                "Configure another archive."
            )

        wiki.save(
            ARCHIVE,
            updated,
            archive_revision,
            f"Archive marked discussions from [[{SOURCE}]] "
            f"(revision {revision})",
        )

    verified, _ = wiki.page(ARCHIVE)

    if not all(record(topic) in verified for topic in selected):
        raise RuntimeError(
            "Archive verification failed. "
            "Source left intact."
        )

    _, latest_revision = wiki.page(SOURCE)

    if latest_revision != revision:
        LOG.warning(
            "Talk page changed; keeping discussions until next run."
        )
        return

    remaining = [
        topic
        for topic, group in zip(topics, calls)
        if not group & aliases
    ]

    wiki.save(
        SOURCE,
        lead + "".join(format_topic(topic) for topic in remaining),
        revision,
        f"Archive {len(selected)} marked discussion(s) "
        f"to [[{ARCHIVE}]]",
    )

    LOG.info("Archiving complete.")


def main():
    parser = argparse.ArgumentParser(
        description="Archive closed discussions on User talk:Larawag."
    )

    parser.add_argument(
        "--run",
        action="store_true",
        help="Enable real Wikipedia edits.",
    )

    parser.add_argument(
        "--login-test",
        action="store_true",
        help="Test login and preview without editing.",
    )

    args = parser.parse_args()
    lock_path = Path(__file__).with_suffix(".lock")

    with lock_path.open("w") as lock:
        try:
            fcntl.flock(
                lock,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            LOG.info("Another instance is running.")
            return

        wiki = Wiki()

        if args.run or args.login_test:
            wiki.login()

        archive(wiki, args.run)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        LOG.error(
            "%s: %s",
            type(error).__name__,
            str(error),
        )
        raise SystemExit(1)
