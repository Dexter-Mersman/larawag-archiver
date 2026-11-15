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
ARCHIVE = SOURCE + "/Archive closed"
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


def template_calls(text):
    """
    Find actual template calls at the beginning of a line,
    optionally preceded by whitespace and discussion indentation.

    Recognizes:
        {{closed}}
        {{closed|reason=Finished}}
        :{{closed}}
        ::{{closed}} Signature and timestamp

    Ignores inline examples such as:
        You can use {{closed}} here.

    Nested template calls and calls inside comments or nowiki
    are not treated as independent closure markers.
    """
    names = set()
    offset = 0

    for node in mw.parse(text).nodes:
        raw = str(node)

        if isinstance(node, Template):
            line_start = text.rfind("\n", 0, offset) + 1
            before = text[line_start:offset]
            title = template_title(node.name)

            if title and not before.replace(":", "").strip():
                names.add(title)

        offset += len(raw)

    return names


def marker(topic):
    """Create a stable identifier for an exact discussion copy."""
    digest = hashlib.sha256(
        (SOURCE + "\n" + topic).encode("utf-8")
    ).hexdigest()

    return "<!-- LarawagArchiver:" + digest + " -->"


def record(topic):
    """Wrap the original discussion in duplicate-detection markers."""
    return (
        marker(topic)
        + "\n"
        + topic
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
        """Resolve observed template names and redirects to Closed."""
        if not names:
            return set()

        target = self.request(
            action="query",
            titles="Template:Closed",
            redirects=1,
        )["query"]["pages"][0]

        if "missing" in target:
            raise RuntimeError(
                "Could not resolve Template:Closed."
            )

        target_id = target.get("pageid")

        if target_id is None:
            raise RuntimeError(
                "Template:Closed has no readable page ID."
            )

        matches = set()

        for name in sorted(names):
            page = self.request(
                action="query",
                titles=name,
                redirects=1,
            )["query"]["pages"][0]

            if page.get("pageid") == target_id:
                matches.add(name)

        return matches

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
            raise RuntimeError(
                "Save was not confirmed."
            )


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

        LOG.info(
            "Topic: %s",
            str(heading.title).strip(),
        )

    if not live or not selected:
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
            old_archive = "{{Talk archive}}\n\n"

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
            f"Archive closed discussions from [[{SOURCE}]] "
            f"(revision {revision})",
        )

    verified, _ = wiki.page(ARCHIVE)

    if not all(
        record(topic) in verified
        for topic in selected
    ):
        raise RuntimeError(
            "Archive verification failed. "
            "Source left intact."
        )

    _, latest_revision = wiki.page(SOURCE)

    if latest_revision != revision:
        LOG.warning(
            "Talk page changed; keeping discussions "
            "until next run."
        )
        return

    remaining = [
        topic
        for topic, group in zip(topics, calls)
        if not group & aliases
    ]

    wiki.save(
        SOURCE,
        lead + "".join(remaining),
        revision,
        f"Archive {len(selected)} closed discussion(s) "
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
