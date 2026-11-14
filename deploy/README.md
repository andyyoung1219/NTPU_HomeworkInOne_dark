# Chromium seccomp policy

`chromium-seccomp.json` starts from Moby's default Docker seccomp profile at commit `f862455628f78f84c937e967e4a03902fb1dee11`. Its first rule adds the namespace calls used by the non-root Chromium sandbox, following Playwright's Docker guidance. Docker Compose applies this policy to the workbench only. Python Runner containers use Docker's default policy.

- [Pinned Moby policy](https://github.com/moby/profiles/blob/f862455628f78f84c937e967e4a03902fb1dee11/seccomp/default.json)
- [Playwright's Chromium sandbox guidance](https://playwright.dev/docs/docker#crawling-and-scraping)
- [Moby license](../LICENSES/moby-profiles.txt)
