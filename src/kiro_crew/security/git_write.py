"""Git write classifier: is a shell command anything other than a known git read?

:func:`git_history_write` answers one question for the dashboard approval ladder
(``dashboard/chat_turn/tool_approval.py``): does this shell command run git in
any way this module cannot show is read-only? When it does,
``hooks.confirm_git_writes`` (on by default) puts the call to the person even in
a session whose grants would otherwise approve it, until the person or the
dispatching session allows git writes for that session.

It is a CONFIRM classifier, never a deny, and it is fail-closed by construction.
A command that names git is a read only when the WHOLE command is one plain git
call (:func:`_is_single_plain_git`: printable ASCII, no pipe, chain,
redirection, group, substitution, expansion, glob, brace, comment, escape,
tilde expansion, runner or environment prefix) and every git it names is

* a git invocation in a command position (directly, after ``VAR=`` prefixes and
  redirections, or after a program runner such as ``env`` or ``timeout``),
* whose argv parsed cleanly -- quoting resolved, no expansion, substitution or
  unquoted glob in it before ``--``, only the global options
  ``--no-pager`` / ``-P`` and ``--no-optional-locks`` (``-C``, ``--git-dir``,
  ``--work-tree``, ``--bare``, ``-p`` and ``--help`` ask), no
  environment assignment other than a short locale/terminal list
  (:data:`_SAFE_ENV_RE`; ``HOME=``, ``GIT_DIR=`` or ``GIT_PAGER=`` asks), not fed
  from ``xargs``, and no signature placeholder (``%G``, ``%(signature``),
* and whose subcommand is on a deliberately SHORT list -- ``status``, ``log``,
  ``diff``, ``show``, ``rev-parse`` (:data:`_READ_OPTIONS`) and
  ``config --get <key>`` / ``config --list`` (every ``git branch`` asks; so does
  ``--version`` unless it is the whole argv, ``git --version``) --
* with every option on that subcommand's own allow-list; an option not listed,
  in any spelling, asks. The list only ever shrinks.

Everything else that names git is a write: any other subcommand (a repository
alias, one this module has never seen, one git adds later), a redirection
anywhere in a command that runs git (other than discarding output or joining
stderr to stdout), any step beside a git read that the shared read-only
classifier does not accept (``git show x | cp /dev/stdin f``), any other command
in a segment that names git (``echo git commit``, ``which git``, ``ls .git``,
``tee .git/HEAD``), text that names git but cannot be read (unbalanced quoting
or substitution, nesting past :data:`_MAX_DEPTH`), and a program word that could
resolve to git without spelling it (an expansion, ANSI-C quoting, a glob, a
brace list, git's dashed ``git-<sub>`` programs read as ``git <sub>``). A
segment that names no git is still checked for that last case at every command
position (after ``if``, ``while``, ``do``, ``{`` ...), and code a shell is handed
(``eval``, ``sh -c``) is read as a command; an ``eval`` or ``sh -c`` body holding
an expansion, and a shell reading commands from its standard input
(``... | sh``), ask.

Backslash-newline pairs are removed before words are read, as bash does. The
text is read twice, with unquoted ``#`` comments removed and as written, and
either reading that finds a non-read decides, so a comment the scanner reads
differently from bash cannot hide a command. Quoted strings, substitutions and
``NAME=value`` assignment values are read again as commands, so
``bash -c 'git commit'`` and ``$(git commit)`` are seen. A nested string is
re-read only when it names git or holds ANSI-C quoting, a glob or a brace list,
so quoted variables in ordinary non-git arguments do not ask.

"Names git" means a git WORD -- ``git``, ``git.exe``, ``git-<something>`` or a
``.git`` path component -- not the letters in ``github.com``. The classifier
reads the command line only. It is not a sandbox: a program the command starts
(a script file, an interpreter running inline code that spells git some other
way) is not opened here, and neither is a program a repository's own
configuration names.

Pure text work, like ``readonly_bash``: nothing is resolved on ``PATH`` and
nothing touches the filesystem. Reached by its own path, not the facade.
"""

from __future__ import annotations

import fnmatch
import re
import shlex

from kiro_crew.security.readonly_bash import (
    _ASSIGNMENT_RE,
    _COMMAND_POSITION_OPENERS,
    _PROGRAM_RUNNERS,
    _REDIRECT_RE,
    _is_git,
    is_read_only_bash,
)

#: The reason returned for text this module cannot read.
UNREADABLE = "unreadable git command"

#: The reason the dashboard ladder records for a tool call that asks to open a
#: session holding the per-session git-writes allow (``session_create``).
GRANT_REQUEST = "allow_git_writes request"

#: How deep quoted strings and substitutions are re-read as commands.
_MAX_DEPTH = 4

_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]|(?<![<>])&(?!>)")

#: Stand-in for a command substitution once its body has been read. It carries an
#: unquoted ``$`` so that, in a git argv, it reads as an expansion.
_SUBST_PLACEHOLDER = " $_kc_subst_ "

#: A word worth re-reading as a command: it holds a separator or an expansion.
_COMMAND_LIKE_RE = re.compile(r"[\s;&|`]|\$\(")

#: Text that can hide a program name from a plain ``git`` substring check.
_OBSCURED_PROGRAM_RE = re.compile(r"\$'|[*?\[]|\{[^}]*,")

# ── What counts as a read ─────────────────────────────────────────────────────

#: git's global options that take a value as the next word (or after ``=``).
#: ``--git-dir`` and ``--work-tree`` are left out: they point git at a
#: configuration this module has not seen, so a git call carrying one asks.
_GLOBAL_VALUE_OPTS: frozenset[str] = frozenset()

#: A word a program runner (``env``, ``timeout``, ``nice`` ...) takes before the
#: program it starts: an option, an assignment, or a number or duration.
_RUNNER_VALUE_RE = re.compile(r"^(?:-.*|[A-Za-z_][A-Za-z0-9_]*=.*|[+-]?\d+(?:\.\d+)?[smhd]?)$")

#: git's global options that take no value and change nothing that writes.
#: ``-p`` / ``--paginate`` (forces a pager) and ``--bare`` (reads the current
#: directory as a repository) are left out, so a call carrying one asks.
_GLOBAL_FLAG_OPTS = frozenset({"--no-pager", "-P", "--no-optional-locks"})

#: git's global options that print and exit. ``--help`` / ``-h`` open a manual
#: viewer and are left out.
_GLOBAL_INFO_OPTS = frozenset({"--version"})


def _opts(text: str) -> frozenset[str]:
    return frozenset(text.split())


#: The ONLY reads. The list is deliberately small -- the subcommands agents use
#: to look at a repository -- and each accepts only the options listed for it.
#: An option missing from its entry, or one git would read as an abbreviation of
#: a listed name, asks; so does every other subcommand. Entries are only ever
#: removed: a review that finds a listed option doing more than reading drops it.
#:
#: Each entry is (long options, short flags, short options whose value follows
#: in the same word or the next one, whether a bare ``-<count>`` is accepted).
_READ_OPTIONS: dict[str, tuple[frozenset[str], str, str, bool]] = {
    "status": (
        _opts("--short --branch --porcelain --long --untracked-files --null"),
        "sbz",
        "u",
        False,
    ),
    "log": (
        _opts("""--oneline --graph --decorate --no-decorate --all
            --stat --shortstat --numstat --name-only --name-status --patch
            --no-patch --max-count --since --until --author --grep --reverse
            --first-parent --merges --no-merges --abbrev-commit --date --follow
            --color --no-color"""),
        "p",
        "n",
        True,
    ),
    "show": (
        _opts("""--stat --shortstat --numstat --name-only --name-status --patch
            --no-patch --oneline --abbrev-commit --date
            --decorate --color --no-color"""),
        "ps",
        "",
        False,
    ),
    "diff": (
        _opts("""--cached --staged --stat --shortstat --numstat --name-only
            --name-status --patch --unified --word-diff --ignore-all-space
            --ignore-space-change --exit-code --quiet --check --merge-base
            --no-ext-diff --no-textconv --color --no-color"""),
        "pwb",
        "U",
        False,
    ),
    "rev-parse": (
        _opts("""--show-toplevel --abbrev-ref --short --verify --quiet --git-dir
            --absolute-git-dir --git-common-dir --is-inside-work-tree
            --show-prefix --show-cdup --symbolic-full-name"""),
        "q",
        "",
        False,
    ),
}

#: Subcommands that are read-only when every option they carry is listed for them.
_PLAIN_READS = frozenset(_READ_OPTIONS)

#: A configuration key as ``git config --get`` reads it. Each middle part holds no
#: dot, so a key splits into parts exactly one way and matching stays linear.
_CONFIG_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*(?:\.[^\s=.]+)*\.[A-Za-z][A-Za-z0-9-]*$")

#: Placeholders in a ``--format`` / ``--pretty`` value that check a signature,
#: which starts the signing program.
_SIGNATURE_FORMAT_RE = re.compile(r"%G|%\(signature", re.I)


def _options_listed(spec: tuple[frozenset[str], str, str, bool], args: list[str]) -> bool:
    """Whether every option in ``args`` (up to ``--``) is listed in ``spec``.

    A long option is matched on its exact name (an abbreviation git would accept
    is not listed, so it asks); ``=value`` is its value. In a short cluster each
    letter must be a listed flag until a listed value option, whose value is the
    rest of the word. Words that do not start with ``-`` are revisions, paths
    and option values.
    """
    longs, flags, valued, counts = spec
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("--"):
            if arg.split("=", 1)[0] not in longs:
                return False
        elif arg.startswith("-") and arg != "-":
            body = arg[1:]
            if counts and body.isdigit():
                continue
            for ch in body:
                if ch in valued:
                    break
                if ch not in flags:
                    return False
    return True


def _config_reads(args: list[str]) -> bool:
    """Only ``git config --get <key>`` and ``git config --list``."""
    return args == ["--list"] or (
        len(args) == 2 and args[0] == "--get" and bool(_CONFIG_KEY_RE.match(args[1]))
    )


def _subcommand_reads(sub: str, args: list[str]) -> bool:
    """Whether ``git <sub> <args>`` is a known read. Case-sensitive, as git is."""
    if any(_SIGNATURE_FORMAT_RE.search(a) for a in args):
        return False
    if sub in _PLAIN_READS:
        return _options_listed(_READ_OPTIONS[sub], args)
    if sub == "config":
        return _config_reads(args)
    return False


def _classify_git_args(args: list[str]) -> str:
    """``""`` when ``git <args>`` is a known read, else the reason it is not."""
    index = 0
    while index < len(args):
        arg = args[index]
        name = arg.split("=", 1)[0]
        if arg in _GLOBAL_VALUE_OPTS:
            if index + 1 >= len(args):
                return UNREADABLE
            index += 2
            continue
        if "=" in arg and name in _GLOBAL_VALUE_OPTS and name.startswith("--"):
            index += 1
            continue
        if arg in _GLOBAL_FLAG_OPTS:
            index += 1
            continue
        if arg in _GLOBAL_INFO_OPTS:
            # A read only as the whole argv (``git --version``): git reads the
            # words after it too (``--help`` wins), so anything else asks.
            if index == 0 and len(args) == 1:
                return ""
            return "git --version with more words"
        if arg.startswith("-"):
            # ``-c``, ``--config-env``, ``--exec-path=`` and anything unknown.
            return "git with a global option it does not read"
        break
    if index >= len(args):
        return ""
    sub = args[index]
    if not sub.strip() or "$" in sub:
        return UNREADABLE
    if _subcommand_reads(sub, args[index + 1 :]):
        return ""
    return f"git {sub}"


# ── Reading the shell ─────────────────────────────────────────────────────────


def _basename(word: str) -> str:
    return word.replace("\\", "/").rsplit("/", 1)[-1]


def _dashed_git_sub(word: str) -> str:
    """``commit`` for ``git-commit`` (git's per-command programs), else ``""``."""
    base = _basename(word)
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base[4:] if base.lower().startswith("git-") and len(base) > 4 else ""


def _is_git_word(word: str) -> bool:
    return _is_git(word) or bool(_dashed_git_sub(word))


#: Most candidates a brace expansion may produce before it stops.
_BRACE_BUDGET = 64

#: What an expansion that ran over its budget reads as: a git name, so the command asks.
_OVER_BUDGET = ["git"]


def _expand_braces(word: str) -> list[str]:
    """A bounded expansion of ``{a,b}`` lists in ``word``, enough to see a program name.

    One budget covers the whole expansion. Running over it, or a list with more
    parts than the budget, returns ``_OVER_BUDGET``, which reads as git.
    """
    out = _expand_braces_within(word, 0, [_BRACE_BUDGET])
    return _OVER_BUDGET if out is None else out


def _expand_braces_within(word: str, _depth: int, budget: list[int]) -> list[str] | None:
    """``_expand_braces`` sharing ``budget``; ``None`` once the budget runs out."""
    start = word.find("{")
    if start < 0 or _depth > 4:
        budget[0] -= 1
        return [word] if budget[0] >= 0 else None
    level = 0
    for end in range(start, len(word)):
        if word[end] == "{":
            level += 1
        elif word[end] == "}":
            level -= 1
            if level == 0:
                break
    else:
        budget[0] -= 1
        return [word] if budget[0] >= 0 else None
    body, head, tail = word[start + 1 : end], word[:start], word[end + 1 :]
    parts, level, last = [], 0, 0
    for i, ch in enumerate(body):
        if ch == "{":
            level += 1
        elif ch == "}":
            level -= 1
        elif ch == "," and level == 0:
            parts.append(body[last:i])
            last = i + 1
    parts.append(body[last:])
    if len(parts) < 2:
        budget[0] -= 1
        return [word] if budget[0] >= 0 else None
    out: list[str] = []
    for part in parts:
        more = _expand_braces_within(head + part + tail, _depth + 1, budget)
        if more is None:
            return None
        out.extend(more)
    return out


#: Names a glob in a program word is tried against.
_GLOB_PROBES = ("git", "git.exe", "git-commit", "git-push", "git-x")


def _could_name_git(word: str) -> bool:
    """Whether this program word, which is not literally git, could still run git."""
    base = _basename(word)
    # A backslash is a path separator on Windows and an escape in bash
    # (``$'\x67it'``), so the last component is read both ways.
    if "$" in base or "$" in word.rsplit("/", 1)[-1]:
        return True
    if any(ch in base for ch in "*?["):
        return any(fnmatch.fnmatchcase(c, base) for c in _GLOB_PROBES)
    if "{" in base and "," in base:
        return any(_is_git_word(c) for c in _expand_braces(base))
    return False


def _find_program(words: list[str], start: int = 0) -> tuple[int | None, list[str], str]:
    """``(index of the program, runners before it, reason)`` for the command that
    starts at ``words[start]``.

    After a program runner (``env``, ``timeout``, ``sudo`` ...) its own options
    and values come before the program it runs, so the first git word after it is
    taken as the program. ``reason`` is :data:`UNREADABLE` for a program word that
    could resolve to git without spelling it.
    """
    runners: list[str] = []
    skip_target = False
    for index in range(start, len(words)):
        word = words[index]
        if runners:
            if _is_git_word(word):
                return index, runners, ""
            if _could_name_git(word):
                return None, runners, UNREADABLE
            if _basename(word).lower() in _PROGRAM_RUNNERS:
                runners.append(_basename(word).lower())
                continue
            if not _RUNNER_VALUE_RE.match(word):
                # A plain word that is not an option, an assignment or a count is
                # the program the runner starts; a git word after it is only its
                # argument. A runner option that takes a word value lands here
                # too, which asks rather than reads.
                return index, runners, ""
            continue
        if skip_target:
            skip_target = False
            continue
        if word in _COMMAND_POSITION_OPENERS or word in _RESTART_AFTER:
            continue
        if _ASSIGNMENT_RE.match(word):
            continue
        redirect = _REDIRECT_RE.match(word)
        if redirect:
            skip_target = redirect.end() == len(word)
            continue
        if _is_git_word(word):
            return index, runners, ""
        if _could_name_git(word):
            return None, runners, UNREADABLE
        program = _basename(word).lower()
        if program in _PROGRAM_RUNNERS:
            runners.append(program)
            continue
        return index, runners, ""
    return None, runners, ""


def _expands_or_globs(segment: str) -> bool:
    """Whether the raw segment holds, before an unquoted ``--``, an unquoted glob
    or a ``$`` outside single quotes -- argv that is not known until the shell runs."""
    quote = ""
    i, n = 0, len(segment)
    while i < n:
        ch = segment[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = ""
            elif ch in "$`":
                return True
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch in "$`*?[":
            return True
        elif (
            ch == "-"
            and segment[i : i + 2] == "--"
            and (i == 0 or segment[i - 1] in " \t")
            and (i + 2 == n or segment[i + 2] in " \t")
        ):
            return False
        i += 1
    return False


#: Redirections a git read may carry and still be a read: output thrown away,
#: or one standard stream joined to the other. Matched on the unquoted text.
_SAFE_REDIRECT_RE = re.compile(
    r"(?:(?<=[\s;(])|^)(?:(?:[12]?>>?|&>>?)[ \t]*/dev/null|[12]?>&[12])(?=[\s;)]|$)"
)


def _unquoted_text(segment: str) -> str:
    """``segment`` with every quoted or backslash-escaped character replaced by ``_``,
    so a ``<`` or ``>`` left in the result is one the shell reads as a redirection."""
    out: list[str] = []
    quote = ""
    i, n = 0, len(segment)
    while i < n:
        ch = segment[i]
        if quote:
            if ch == quote:
                quote = ""
                out.append("_")
            elif quote == '"' and ch == "\\" and i + 1 < n:
                out.append("__")
                i += 2
                continue
            else:
                out.append("_")
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append("__")
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append("_")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _has_unlisted_redirect(segment: str) -> bool:
    """Whether the segment holds a redirection that is not on the safe list.

    The shell opens a redirection's target before git starts, so ``git status >
    .git/HEAD`` writes whatever git then does. Only discarding output or joining
    stderr to stdout leaves a read a read; any other ``<`` or ``>``, including a
    here-document or a target the scanner cannot read, asks.
    """
    text = _SAFE_REDIRECT_RE.sub(" ", _unquoted_text(segment))
    return "<" in text or ">" in text


def _git_reason(words: list[str], index: int, runners: list[str], segment: str) -> str:
    """The reason the git invocation at ``words[index]`` is not a known read."""
    if "xargs" in runners:
        return "git arguments read from input"
    if "command" in runners and any(w in ("-v", "-V") for w in words[:index]):
        return "git path looked up"
    for word in words[:index]:
        if _ASSIGNMENT_RE.match(word) and not _SAFE_ENV_RE.match(word):
            # Only a short list of environment names leaves a git read a read;
            # any other one may change the config, repository or programs git uses.
            return "git with a configuration override"
    if _expands_or_globs(segment):
        return UNREADABLE
    sub = _dashed_git_sub(words[index])
    args = [sub, *words[index + 1 :]] if sub else words[index + 1 :]
    reason = _classify_git_args(args)
    if not reason and _has_unlisted_redirect(segment):
        return "git with a redirection"
    return reason


#: Environment assignments a git read may carry and still be a read.
_SAFE_ENV_RE = re.compile(
    r"^(?:LANG|LANGUAGE|LC_\w+|TZ|TERM|NO_COLOR|COLUMNS|LINES|GIT_TERMINAL_PROMPT"
    r"|GIT_OPTIONAL_LOCKS)=",
)

#: Words after which bash starts a new command in the same segment.
_RESTART_AFTER = frozenset(
    {
        "(",
        ")",
        "{",
        "}",
        "!",
        "then",
        "do",
        "else",
        "elif",
        "if",
        "while",
        "until",
        "coproc",
        "time",
        "in",
    }
)

#: Programs that run their arguments, or their standard input, as shell code.
_SHELL_RUNNERS = frozenset(
    {"eval", "sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "ash", "busybox", "source", "."}
)

#: A git word in de-quoted text: ``git``, ``/usr/bin/git``, ``git-commit``, ``.git``.
#: Not ``github`` or ``gitlab``.
_GIT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])git(?![A-Za-z0-9_])", re.I)

#: The user's git configuration file, where a later git read takes its
#: configuration (and so the programs it runs) from.
_GITCONFIG_RE = re.compile(r"\.gitconfig(?![A-Za-z0-9_])", re.I)

#: Words that only open or close a group, subshell or compound command.
_GROUPING_WORDS_RE = re.compile(r"^(?:\s*(?:\{|\}|\(|\)|!|done|fi|esac|then|do|else)(?=\s|$))+")

#: Steps beside a git read that the shared read-only classifier does not list
#: but that read nothing and write nothing.
_HARMLESS_STEP_RE = re.compile(r"^(?:cd(?:\s+[^\s;&|<>$`]+)?|true|false|:)$")


def _is_known_read_step(segment: str) -> bool:
    """Whether a segment that sits beside a git read (a pipeline stage taking its
    output, a command before or after it) is itself a known read.

    Positive: the segment must be empty once grouping words and the safe
    redirections are removed, a harmless step (``cd <dir>``, ``true``), or a
    command :func:`readonly_bash.is_read_only_bash` accepts. Anything else --
    ``cp /dev/stdin f``, ``sort -o f``, ``command tee f``, ``sh -c '...'`` -- asks.
    """
    text = _SAFE_REDIRECT_RE.sub(" ", segment)
    text = _GROUPING_WORDS_RE.sub("", text).strip()
    if not text:
        return True
    if _HARMLESS_STEP_RE.match(text):
        return True
    return is_read_only_bash(text)


def _dequote(text: str) -> str:
    return re.sub(r"[\\'\"]", "", text)


def _names_git(segment: str, words: list[str]) -> bool:
    """Whether a segment mentions git: a git word, ANSI-C quoting, or a word whose
    glob or brace list could expand to a git name."""
    if (
        _GIT_TOKEN_RE.search(_dequote(segment))
        or _GITCONFIG_RE.search(_dequote(segment))
        or "$'" in segment
    ):
        return True
    for word in words:
        base = _basename(word)
        if (
            "{" in base
            and "," in base
            and any(_GIT_TOKEN_RE.search(c) for c in _expand_braces(base))
        ):
            return True
        if any(ch in base for ch in "*?[") and re.sub(r"[*?\[\]!^\-]", "", base):
            if any(fnmatch.fnmatchcase(c, base) for c in _GLOB_PROBES):
                return True
    return False


def _clean_read_reason(words: list[str], segment: str) -> str:
    """For a segment that mentions git: ``""`` only when it is one clean git read."""
    index, runners, reason = _find_program(words)
    if reason:
        return reason
    if index is None or not _is_git_word(words[index]):
        return "git passed to another program"
    return _git_reason(words, index, runners, segment)


def _git_free_reason(words: list[str], depth: int) -> str:
    """For a segment that names no git: whether a program it runs could still be git."""
    starts = [0] + [i + 1 for i, w in enumerate(words) if w in _RESTART_AFTER]
    for start in starts:
        if start >= len(words):
            continue
        index, _runners, reason = _find_program(words, start)
        if reason:
            return reason
        if index is None:
            continue
        program = _basename(words[index]).lower()
        if program == "alias":
            continue
        if program in _SHELL_RUNNERS:
            code = [w for w in words[index + 1 :] if not w.startswith("-")]
            if program not in ("eval", "source", ".") and "-c" not in words[index + 1 :]:
                if not code:
                    # A shell reading its commands from standard input.
                    return UNREADABLE
                continue  # a script file, which is not opened here
            if any("$" in w or "`" in w for w in code):
                return UNREADABLE
            for body in code:
                reason = _reason(body, depth + 1, force=True)
                if reason:
                    return reason
    for word in words:
        if _ASSIGNMENT_RE.match(word):
            reason = _reason(word.split("=", 1)[1], depth + 1)
        elif _COMMAND_LIKE_RE.search(word):
            reason = _reason(word, depth + 1)
        else:
            continue
        if reason:
            return reason
    return ""


def _words_reason(words: list[str], depth: int, segment: str) -> str:
    """The reason one segment is not a known git read, or ``""``.

    Fail-closed by construction: a segment that mentions git is a read only when
    it is exactly one clean git read; a segment that does not mention git is
    checked only for a program that could still resolve to git.
    """
    if _names_git(segment, words):
        return _clean_read_reason(words, segment)
    return _git_free_reason(words, depth)


def _strip_substitutions(cmd: str, depth: int) -> tuple[str, str]:
    """Read every command / process substitution; return ``(outer, reason)``.

    The outer text comes back with each substitution replaced by
    :data:`_SUBST_PLACEHOLDER`. ``reason`` is the first non-read found inside one,
    or :data:`UNREADABLE` when a substitution does not close.
    """
    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        opener = cmd[i : i + 2]
        if opener in ("$(", "<(", ">("):
            level = 1
            j = i + 2
            while j < n and level:
                if cmd[j] == "(":
                    level += 1
                elif cmd[j] == ")":
                    level -= 1
                j += 1
            if level:
                return "", UNREADABLE
            reason = _reason(cmd[i + 2 : j - 1], depth + 1)
            if reason:
                return "", reason
            out.append(_SUBST_PLACEHOLDER)
            i = j
            continue
        if ch == "`":
            j = cmd.find("`", i + 1)
            if j < 0:
                return "", UNREADABLE
            reason = _reason(cmd[i + 1 : j], depth + 1)
            if reason:
                return "", reason
            out.append(_SUBST_PLACEHOLDER)
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), ""


def _join_continuations(cmd: str) -> str:
    """Remove backslash-newline pairs, which bash deletes before reading words.

    Done everywhere, including inside single quotes where bash keeps them: that
    can only join more text into a word, which reads more, never less.
    """
    return cmd.replace("\\\r\n", "").replace("\\\n", "")


def _strip_comments(cmd: str) -> str:
    """Drop each unquoted ``#`` comment that starts a word, to the end of its line."""
    out: list[str] = []
    quote = ""
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(cmd[i : i + 2])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|()<>"):
            newline = cmd.find("\n", i)
            if newline < 0:
                break
            i = newline
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _could_run_git(text: str) -> bool:
    """Whether text this module cannot read could still run git (or set what a
    later git read runs, through ``.gitconfig``)."""
    plain = _dequote(text)
    return bool(
        _GIT_TOKEN_RE.search(plain)
        or _GITCONFIG_RE.search(plain)
        or _OBSCURED_PROGRAM_RE.search(text)
    )


def _reason(cmd: str, depth: int, *, force: bool = False) -> str:
    cmd = _join_continuations(cmd)
    if depth > _MAX_DEPTH:
        return UNREADABLE if _could_run_git(cmd) or "$" in cmd else ""
    if depth and not force and not _could_run_git(cmd):
        # A nested string (an argument, a quoted body, an assignment value)
        # that cannot name git is not read as a command. The top level, and
        # code a shell runs (``eval``, ``sh -c``), is always read.
        return ""
    # Read twice: with unquoted ``#`` comments removed as bash does, and as
    # written. Either reading that finds a non-read decides; reading more text
    # can only ask more, so a comment scanner that disagrees with bash cannot
    # hide a command.
    for text in (_strip_comments(cmd), cmd):
        reason = _reason_once(text, depth)
        if reason:
            return reason
    return ""


def _reason_once(cmd: str, depth: int) -> str:
    outer, reason = _strip_substitutions(cmd, depth)
    if reason == UNREADABLE:
        return UNREADABLE if _could_run_git(cmd) else ""
    if reason:
        return reason
    segments = _SEGMENT_SPLIT_RE.split(outer.replace("\r", "\n"))
    for segment in segments:
        spaced = segment.replace("(", " ( ").replace(")", " ) ")
        try:
            words = shlex.split(spaced)
        except ValueError:
            if _could_run_git(segment):
                return UNREADABLE
            continue
        # POSIX quote removal eats a Windows path's backslashes, so the raw
        # words are read too, with their quote characters dropped -- the same
        # two readings ``command_invokes_git`` takes.
        raw = [word.strip("'\"") for word in spaced.split()]
        for candidate in (words, raw):
            reason = _words_reason(candidate, depth, segment)
            if reason:
                return reason
    plain = _dequote(cmd)
    if _GIT_TOKEN_RE.search(plain) or _GITCONFIG_RE.search(plain):
        # Read on the text BEFORE substitutions are set aside, so a git call
        # inside ``$(...)`` still counts. A redirection anywhere in a command
        # that runs git -- after a group's closing word, set by ``exec``, or
        # around a substitution -- may write what git printed, so it asks. Only
        # discarding output and joining stderr to stdout leave a read a read.
        if _has_unlisted_redirect(cmd):
            return "git with a redirection"
        # Every segment above that names git is a proven read. Every other one
        # -- a pipeline stage that takes git's output, a command before or
        # after it -- must be a known read too (``head``, ``grep``, ``wc`` ...,
        # judged by the shared read-only classifier), or the command asks.
        for segment in segments:
            if _GIT_TOKEN_RE.search(_dequote(segment)):
                continue
            if not _is_known_read_step(segment):
                return "git with a command that is not a known read"
    return ""


def git_history_write(cmd: str) -> str:
    """Why ``cmd`` is not a known git read, or ``""`` when it is one (or runs no git).

    A non-empty answer is a short reason (``"git commit"``, ``"git fetch"``,
    :data:`UNREADABLE`) fit for an audit row; it never quotes the command.
    """
    if not cmd:
        return ""
    reason = _reason(cmd, 0)
    if reason:
        return reason
    plain = _dequote(cmd)
    if (
        _GIT_TOKEN_RE.search(plain) or _GITCONFIG_RE.search(plain) or "$'" in cmd
    ) and not _is_single_plain_git(cmd):
        return NOT_SINGLE_READ
    return ""


#: The reason for a command that names git and is anything but one plain git call.
NOT_SINGLE_READ = "git command that is not a single plain read"

#: Characters with a shell meaning outside quotes: pipes, chains, redirects,
#: groups, substitution and expansion, globs, braces, comments, escapes and
#: history. ``~`` is checked by position: bash expands it at the start of a word
#: or after ``=`` / ``:``, but ``HEAD~1`` is literal.
_SHELL_META = frozenset("|&;<>(){}$`*?[]#\\!\n\r")

#: Characters with a shell meaning inside double quotes.
_DQUOTE_META = frozenset("$`\\!\n\r")


def _is_single_plain_git(cmd: str) -> bool:
    """Whether ``cmd`` is exactly one plain git call: printable ASCII, words split
    by spaces or tabs only, quotes used only to hold literal text, and nothing
    the shell would act on -- no pipe, chain, redirection, group, substitution,
    expansion, glob, brace, comment, escape or tilde expansion -- with the bare
    word ``git`` as its first word (not a path, not another case).

    This is the shape a known read must have; the subcommand and its options
    are judged by the reader above. Everything else that names git asks.
    """
    if any(not (" " <= ch <= "~" or ch == "\t") for ch in cmd):
        return False
    quote = ""
    prev = " "
    for ch in cmd:
        if quote == "'":
            if ch == "'":
                quote = ""
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif ch in _DQUOTE_META:
                return False
        elif ch in ("'", '"'):
            quote = ch
        elif ch in _SHELL_META:
            return False
        elif ch == "~" and prev in " \t=:":
            return False
        prev = ch
    if quote:
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    return bool(words) and words[0] == "git"
