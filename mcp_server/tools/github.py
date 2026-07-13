"""Deterministic GitHub workflows built on Kim's existing tools."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from typing import Any

from mcp_server.os_utils import minimal_subprocess_env
from mcp_server.tools import web

logger = logging.getLogger(__name__)

SUCCESS_CREATED = "SUCCESS_CREATED"
SUCCESS_ALREADY_EXISTS = "SUCCESS_ALREADY_EXISTS"
FAIL_GH_NOT_AUTHENTICATED_AND_BROWSER_FAILED = "FAIL_GH_NOT_AUTHENTICATED_AND_BROWSER_FAILED"
FAIL_REPO_NAME_INVALID = "FAIL_REPO_NAME_INVALID"
FAIL_VISIBILITY_NOT_CONFIRMED = "FAIL_VISIBILITY_NOT_CONFIRMED"
FAIL_SUBMIT_NOT_FOUND = "FAIL_SUBMIT_NOT_FOUND"
FAIL_SUBMIT_DISABLED = "FAIL_SUBMIT_DISABLED"
FAIL_VERIFY_FAILED = "FAIL_VERIFY_FAILED"


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _json_result(payload: dict[str, Any], *, debug: bool = False) -> str:
    public = {
        "success": bool(payload.get("success")),
        "code": payload.get("code", ""),
        "summary": payload.get("summary", ""),
        "method": payload.get("method", ""),
        "name": payload.get("name", ""),
        "visibility": payload.get("visibility", ""),
        "url": payload.get("url", ""),
    }
    if not public["success"]:
        public["error"] = public["summary"]
    if debug:
        public["debug"] = {
            "attempts": payload.get("attempts", []),
            "diagnostics": payload.get("diagnostics", {}),
            "details": payload.get("details", {}),
        }
    return json.dumps(public, indent=2)


def _result(
    *,
    success: bool,
    code: str,
    summary: str,
    method: str,
    name: str,
    visibility: str,
    url: str = "",
    attempts: list[dict[str, Any]] | None = None,
    diagnostics: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "success": success,
        "code": code,
        "summary": summary,
        "method": method,
        "name": name,
        "visibility": visibility,
        "url": url,
        "attempts": attempts or [],
        "diagnostics": diagnostics or {},
        "details": details or {},
    }
    logger.info("github_create_repo result: %s", json.dumps(payload, default=str)[:2000])
    return payload


def _valid_repo_name(name: str) -> bool:
    if not 1 <= len(name) <= 100:
        return False
    if name in {".", ".."}:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", name))


def _run_gh(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    # S4: minimal allowlist env, not the full parent env. gh reads its auth
    # from ~/.config/gh (HOME is allowlisted); the token vars are passed
    # through explicitly because handing them to the gh CLI is their purpose.
    env = minimal_subprocess_env(
        extra_keys=("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"),
        overrides={"GH_PROMPT_DISABLED": "1"},
    )
    try:
        return subprocess.run(
            ["gh", *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # A hung gh must degrade like any other gh failure (returncode != 0)
        # so callers' browser-fallback path still engages instead of the
        # whole tool failing hard on an uncaught exception (M5).
        return subprocess.CompletedProcess(
            args=["gh", *args],
            returncode=124,
            stdout="",
            stderr=f"gh timed out after {timeout}s",
        )


async def _run_gh_async(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(_run_gh, args, timeout)


def _parse_github_url(text: str) -> str:
    match = re.search(r"https://github\.com/[^\s]+", text or "")
    return match.group(0).rstrip(".,)") if match else ""


async def _repo_url_from_gh(name: str) -> str:
    view = await _run_gh_async(["repo", "view", name, "--json", "url", "-q", ".url"], timeout=20)
    return view.stdout.strip() if view.returncode == 0 else ""


async def _try_gh_cli(
    name: str,
    description: str,
    visibility: str,
    open_in_browser: bool,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    if not shutil.which("gh"):
        return _result(
            success=False,
            code=FAIL_GH_NOT_AUTHENTICATED_AND_BROWSER_FAILED,
            summary="gh CLI is not installed or not on PATH.",
            method="gh_cli",
            name=name,
            visibility=visibility,
            attempts=attempts,
        )

    auth = await _run_gh_async(["auth", "status", "--hostname", "github.com"], timeout=15)
    attempts.append({"state": "CHECK_GH_AUTH", "returncode": auth.returncode, "stderr": auth.stderr.strip()})
    if auth.returncode != 0:
        return _result(
            success=False,
            code=FAIL_GH_NOT_AUTHENTICATED_AND_BROWSER_FAILED,
            summary="gh CLI is not authenticated for github.com.",
            method="gh_cli",
            name=name,
            visibility=visibility,
            attempts=attempts,
        )

    cmd = ["repo", "create", name, f"--{visibility}"]
    if description:
        cmd.extend(["--description", description])
    create = await _run_gh_async(cmd, timeout=45)
    attempts.append(
        {
            "state": "CREATE_WITH_GH",
            "returncode": create.returncode,
            "stdout": create.stdout.strip(),
            "stderr": create.stderr.strip(),
        }
    )

    combined = "\n".join([create.stdout, create.stderr])
    if create.returncode != 0:
        if re.search(r"\balready exists\b|\bname already exists\b", combined, re.IGNORECASE):
            repo_url = await _repo_url_from_gh(name)
            return _result(
                success=True,
                code=SUCCESS_ALREADY_EXISTS,
                summary=f"Repository {name!r} already exists.",
                method="gh_cli",
                name=name,
                visibility=visibility,
                url=repo_url,
                attempts=attempts,
            )
        return _result(
            success=False,
            code=FAIL_VERIFY_FAILED,
            summary="gh repo create failed.",
            method="gh_cli",
            name=name,
            visibility=visibility,
            attempts=attempts,
            details={"stdout": create.stdout.strip(), "stderr": create.stderr.strip()},
        )

    repo_url = _parse_github_url(combined) or await _repo_url_from_gh(name)
    open_result = ""
    if open_in_browser and repo_url:
        open_result = await web.handle_web_open({"url": repo_url})
        attempts.append({"state": "OPEN_CREATED_REPO", "result": open_result[:500]})

    return _result(
        success=True,
        code=SUCCESS_CREATED,
        summary=f"Created {visibility} GitHub repository {name!r}.",
        method="gh_cli",
        name=name,
        visibility=visibility,
        url=repo_url,
        attempts=attempts,
    )


async def _observe_for_browser_flow(attempts: list[dict[str, Any]], state: str) -> None:
    result = await web.handle_web_observe({"limit": 160})
    attempts.append({"state": state, "result": result[:1000]})


def _meta(element_id: str | None) -> dict[str, Any]:
    if not element_id:
        return {}
    return web._element_data_map.get(str(element_id), {})


def _is_selected(meta: dict[str, Any]) -> bool:
    return bool(meta.get("checked"))


def _page_text_indicates_existing(text: str, name: str) -> bool:
    lower = text.lower()
    return name.lower() in lower and any(
        phrase in lower
        for phrase in (
            "already exists",
            "already been taken",
            "is unavailable",
            "name is already",
        )
    )


async def _try_browser(
    name: str,
    description: str,
    visibility: str,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []

    open_result = await web.handle_web_open({"url": "https://github.com/new"})
    attempts.append({"state": "OPEN_NEW_REPO_PAGE", "result": open_result[:500]})
    if open_result.startswith(("AUTH_REQUIRED:", "AUTH_FAILED:", "ERROR:")):
        return _result(
            success=False,
            code=FAIL_GH_NOT_AUTHENTICATED_AND_BROWSER_FAILED,
            summary="Could not open GitHub new repository page.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
        )

    await _observe_for_browser_flow(attempts, "OBSERVE_INITIAL_FORM")
    repo_name = web._resolve_element(
        "repository name textbox",
        preferred_roles=["textbox", "input"],
        label_hints=["Repository name", "Repository name *", "repo name"],
        require_enabled=True,
        mode="normal",
    )
    attempts.append({"state": "RESOLVE_REPO_NAME_FIELD", "result": repo_name})
    repo_name_id = repo_name.get("element_id")
    if not repo_name.get("ok") or not repo_name_id:
        return _result(
            success=False,
            code=FAIL_VERIFY_FAILED,
            summary="Repository name field was not found.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    scope = web._scope_for_element(str(repo_name_id))
    attempts.append({"state": "CAPTURE_FORM_SCOPE", "scope": scope})

    fill_result = await web.handle_web_fill({"element_id": repo_name_id, "text": name})
    attempts.append({"state": "FILL_REPO_NAME", "result": fill_result})
    if fill_result.startswith("ERROR:"):
        return _result(
            success=False,
            code=FAIL_VERIFY_FAILED,
            summary="Repository name field could not be filled.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    await _observe_for_browser_flow(attempts, "OBSERVE_AFTER_REPO_NAME")
    repo_name_after = web._resolve_element(
        "repository name textbox",
        preferred_roles=["textbox", "input"],
        label_hints=["Repository name", "Repository name *", "repo name"],
        require_enabled=True,
        mode="normal",
        scope=scope,
    )
    repo_name_after_id = repo_name_after.get("element_id")
    value_after = str(_meta(str(repo_name_after_id)).get("value") or "") if repo_name_after_id else ""
    attempts.append({"state": "CONFIRM_REPO_NAME_FILLED", "element_id": repo_name_after_id, "value": value_after})
    if value_after != name:
        return _result(
            success=False,
            code=FAIL_VERIFY_FAILED,
            summary="Repository name field did not retain the requested value.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    page = await web._page()
    page_text = ""
    try:
        page_text = await page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:
        page_text = ""
    if _page_text_indicates_existing(page_text, name):
        return _result(
            success=True,
            code=SUCCESS_ALREADY_EXISTS,
            summary=f"Repository name {name!r} is already in use on GitHub.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    if description:
        description_field = web._resolve_element(
            "repository description textbox",
            preferred_roles=["textbox", "input"],
            label_hints=["Description", "Description (optional)", "Repository description"],
            require_enabled=True,
            mode="normal",
            scope=scope,
        )
        attempts.append({"state": "RESOLVE_DESCRIPTION_FIELD", "result": description_field})
        if description_field.get("ok") and description_field.get("element_id"):
            description_fill = await web.handle_web_fill(
                {"element_id": description_field["element_id"], "text": description}
            )
            attempts.append({"state": "FILL_DESCRIPTION", "result": description_fill})
            await _observe_for_browser_flow(attempts, "OBSERVE_AFTER_DESCRIPTION")
        else:
            attempts.append({"state": "SKIP_DESCRIPTION", "reason": "description field not resolved"})

    visibility_control = web._resolve_element(
        f"{visibility} visibility control",
        preferred_roles=["radio", "button", "input"],
        label_hints=[visibility, visibility.title()],
        text_hints=[visibility, visibility.title()],
        require_enabled=True,
        mode="normal",
        scope=scope,
    )
    attempts.append({"state": "RESOLVE_VISIBILITY_CONTROL", "result": visibility_control})
    visibility_id = visibility_control.get("element_id")
    if not visibility_control.get("ok") or not visibility_id:
        return _result(
            success=False,
            code=FAIL_VISIBILITY_NOT_CONFIRMED,
            summary=f"Requested visibility {visibility!r} could not be resolved in the repository form.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    visibility_meta = _meta(str(visibility_id))
    if not _is_selected(visibility_meta):
        visibility_click = await web.handle_web_click({"element_id": visibility_id})
        attempts.append({"state": "SET_VISIBILITY", "result": visibility_click})
        if visibility_click.startswith("ERROR:"):
            return _result(
                success=False,
                code=FAIL_VISIBILITY_NOT_CONFIRMED,
                summary=f"Requested visibility {visibility!r} could not be selected.",
                method="browser",
                name=name,
                visibility=visibility,
                attempts=attempts,
                diagnostics=web._last_form_diagnostics,
            )
        await _observe_for_browser_flow(attempts, "OBSERVE_AFTER_VISIBILITY")
        visibility_control = web._resolve_element(
            f"{visibility} visibility control",
            preferred_roles=["radio", "button", "input"],
            label_hints=[visibility, visibility.title()],
            text_hints=[visibility, visibility.title()],
            require_enabled=True,
            mode="normal",
            scope=scope,
        )
        visibility_id = visibility_control.get("element_id")
        visibility_meta = _meta(str(visibility_id)) if visibility_id else {}

    attempts.append(
        {
            "state": "CONFIRM_VISIBILITY_SELECTED",
            "element_id": visibility_id,
            "checked": bool(visibility_meta.get("checked")),
        }
    )
    if not visibility_id or not _is_selected(visibility_meta):
        return _result(
            success=False,
            code=FAIL_VISIBILITY_NOT_CONFIRMED,
            summary=f"Requested visibility {visibility!r} was not confirmed selected.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    create_button = web._resolve_element(
        "create repository button",
        preferred_roles=["button"],
        label_hints=["Create repository", "Create repository button"],
        text_hints=["Create repository"],
        require_enabled=False,
        mode="strict",
        scope=scope,
    )
    attempts.append({"state": "RESOLVE_CREATE_BUTTON", "result": create_button})
    create_id = create_button.get("element_id")
    if not create_button.get("ok") or not create_id:
        return _result(
            success=False,
            code=FAIL_SUBMIT_NOT_FOUND,
            summary="Create repository submit button was not found inside the repository form.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    button_meta = _meta(str(create_id))
    if button_meta.get("disabled"):
        return _result(
            success=False,
            code=FAIL_SUBMIT_DISABLED,
            summary="Create repository submit button is disabled; not clicking.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    old_url = page.url
    click_result = await web.handle_web_click({"element_id": create_id})
    attempts.append({"state": "CLICK_CREATE_BUTTON", "result": click_result})
    if click_result.startswith("ERROR:"):
        return _result(
            success=False,
            code=FAIL_VERIFY_FAILED,
            summary="Create repository submit button click failed.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    try:
        await page.wait_for_url(lambda url: str(url) != old_url and "/new" not in str(url), timeout=20000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=7000)
    except Exception:
        pass

    final_url = page.url
    final_text = ""
    try:
        final_text = await page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:
        final_text = ""

    success = "github.com" in final_url and f"/{name}" in final_url and "/new" not in final_url
    if not success:
        success = "Quick setup" in final_text and name in final_text

    attempts.append({"state": "CONFIRM_REPO_CREATED", "final_url": final_url, "success": success})
    if not success:
        return _result(
            success=False,
            code=FAIL_VERIFY_FAILED,
            summary="Could not verify final repository URL or page text.",
            method="browser",
            name=name,
            visibility=visibility,
            attempts=attempts,
            diagnostics=web._last_form_diagnostics,
        )

    return _result(
        success=True,
        code=SUCCESS_CREATED,
        summary=f"Created {visibility} GitHub repository {name!r}.",
        method="browser",
        name=name,
        visibility=visibility,
        url=final_url,
        attempts=attempts,
        diagnostics=web._last_form_diagnostics,
    )


async def handle_github_create_repo(args: dict) -> str:
    name = str(args.get("name", "")).strip()
    visibility = str(args.get("visibility") or "private").strip().lower()
    debug = _as_bool(args.get("debug"), False)

    if not _valid_repo_name(name):
        return _json_result(
            _result(
                success=False,
                code=FAIL_REPO_NAME_INVALID,
                summary="Repository name is invalid. Use 1-100 letters, numbers, dots, hyphens, or underscores.",
                method="none",
                name=name,
                visibility=visibility or "private",
            ),
            debug=debug,
        )

    description = str(args.get("description") or "").strip()
    if visibility not in {"public", "private"}:
        return _json_result(
            _result(
                success=False,
                code=FAIL_REPO_NAME_INVALID,
                summary="visibility must be 'public' or 'private'.",
                method="none",
                name=name,
                visibility=visibility,
            ),
            debug=debug,
        )

    prefer_browser = _as_bool(args.get("prefer_browser"), False)
    open_in_browser = _as_bool(args.get("open_in_browser"), False)

    if not prefer_browser:
        cli_result = await _try_gh_cli(name, description, visibility, open_in_browser)
        if cli_result.get("success"):
            return _json_result(cli_result, debug=debug)
        logger.info(
            "github_create_repo falling back to browser after gh_cli failure: %s",
            cli_result.get("summary", ""),
        )

    browser_result = await _try_browser(name, description, visibility)
    return _json_result(browser_result, debug=debug)
