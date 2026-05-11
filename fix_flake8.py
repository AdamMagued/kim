def fix_browser_provider():
    path = "orchestrator/providers/browser_provider.py"
    with open(path, "r") as f:
        lines = f.readlines()

    for i in range(len(lines)):
        line = lines[i]

        if line.startswith("import uuid"):
            lines[i] = ""
            continue

        # W293, W291
        if line.endswith(" \n") or line.endswith("\t\n") or line.strip() == "":
            lines[i] = line.rstrip(" \t\n") + "\n"

    # E501
    lines[579] = lines[579].replace(
        'browser.new_context(viewport={"width": 1280, "height": 800}, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")',
        'browser.new_context(\n                viewport={"width": 1280, "height": 800},\n                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"\n            )'
    )

    lines[1229] = lines[1229].replace(
        'await self._page.evaluate(\'\'\'() => { const b = document.querySelector(\'button[aria-label="Submit"]\'); if(b) b.click(); }\'\'\')',
        'await self._page.evaluate(\n                \'\'\'() => { const b = document.querySelector(\\\'button[aria-label="Submit"]\\\'); if(b) b.click(); }\'\'\'\n            )'
    )

    lines[1311] = lines[1311].replace(
        'if b_box and submit_box and b_box["y"] == submit_box["y"] and b_box["x"] == submit_box["x"]:',
        'if (\n                            b_box and submit_box and b_box["y"] == submit_box["y"] \n                            and b_box["x"] == submit_box["x"]\n                        ):'
    )

    lines[1322] = lines[1322].replace(
        'await self._page.evaluate(\'\'\'(el) => { if(el) el.click(); }\'\'\', send_buttons[0])',
        'await self._page.evaluate(\n                            \'\'\'(el) => { if(el) el.click(); }\'\'\', send_buttons[0]\n                        )'
    )

    lines[1326] = lines[1326].replace(
        'await self._page.evaluate(\'\'\'(el) => { if(el) el.click(); }\'\'\', submit_buttons[0])',
        'await self._page.evaluate(\n                            \'\'\'(el) => { if(el) el.click(); }\'\'\', submit_buttons[0]\n                        )'
    )

    lines[1335] = lines[1335].replace(
        'logger.info(f"Typing text into file input {selector} (no files provided)")',
        'logger.info(\n                    f"Typing text into file input {selector} (no files provided)"\n                )'
    )

    lines[1448] = lines[1448].replace(
        'await self._page.evaluate(\'\'\'(selector) => { const el = document.querySelector(selector); if(el) el.click(); }\'\'\', selector)',
        'await self._page.evaluate(\n                \'\'\'(selector) => { const el = document.querySelector(selector); if(el) el.click(); }\'\'\', selector\n            )'
    )

    lines[1472] = lines[1472].replace(
        'await self._page.evaluate(\'\'\'(selector) => { const el = document.querySelector(selector); if(el) el.click(); }\'\'\', selector)',
        'await self._page.evaluate(\n                \'\'\'(selector) => { const el = document.querySelector(selector); if(el) el.click(); }\'\'\', selector\n            )'
    )

    lines[1429] = "    async def _wait_for_new_response(\n"

    with open(path, "w") as f:
        f.writelines(lines)

fix_browser_provider()
