# Putting EveWatch on GitHub and publishing installers

## One-time: create the repository
1. On github.com click **New repository**. Name it e.g. `evewatch`. Leave
   "Add a README" etc. **unticked** (this folder already has them). Choose
   Private or Public.
2. In this folder (PowerShell or a terminal):

```
git init -b master
git add .
git status          # check: evewatch_data must NOT be listed
git commit -m "EveWatch 1.1.0"
git remote add origin https://github.com/YOUR-USER/evewatch.git
git push -u origin master
```

`evewatch_data/` (your tokens, Client ID, caches) is excluded by `.gitignore`.
Git will ask you to sign in the first time (browser login or a personal
access token - Git Credential Manager handles it).

## Every release
1. Edit `CHANGELOG.md`. (The version number itself is stamped in from the tag.)
2. Commit, then tag and push:

```
git add . && git commit -m "Release 1.2.0"
git tag v1.2.0
git push && git push origin v1.2.0
```

3. GitHub Actions builds the Windows installer (`EveWatch-Setup-1.2.0.exe`)
   and the Linux package (`EveWatch-linux-x86_64-1.2.0.tar.gz`) and attaches
   them to a new Release (Actions tab shows progress, ~5-10 minutes).
4. Installed copies see it under **Help > Check for updates** and offer the
   download. Installing over the top keeps all data and logins.

**Private repository note:** the in-app update check reads the public GitHub
API, so it only works if the repository (or at least its Releases) is public.
For a private repo, publish releases on a small public repo instead and set
`"update_repo": "user/that-repo"` in `config.json`, or make the repo public.

## Test build without releasing
Actions tab > "Build installers" > **Run workflow**. The installers appear as
downloadable artifacts on that run.
