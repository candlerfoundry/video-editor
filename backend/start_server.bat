@echo off
cd /d "%~dp0"
echo Starting Foundry Video Editor backend...
C:\Python314\python.exe server.py
pause
```

Save and commit directly on GitHub. Netlify will auto-deploy in 30 seconds, then download the new bat file and try again.

---

**For your next Claude Code session**, the prompt to paste is:
```
Pull the context doc from Airtable Claude Artifacts table, record Name: 'Video Editor Web App — Context Document', field: Claude Context Doc. Follow the SESSION 2 instructions exactly.
