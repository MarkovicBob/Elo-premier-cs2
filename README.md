# EloPremier

Streaming overlay that shows a CS2 Premier rating live: Steam sign-in, automatic ELO tracking, daily win/loss record and 10 overlay styles for OBS, Streamlabs and TikTok Live. Available in English and Serbian.

**Stack:** Python · FastAPI · SQLAlchemy · JavaScript · Steam OpenID

## Screenshots

<a href="https://portfoliobob.netlify.app/projects/elo/demo.mp4"><img src="https://portfoliobob.netlify.app/projects/elo/poster.webp" alt="Demo video" width="100%"></a>

<sub>▶ Click the image to watch the demo video · more on <a href="https://portfoliobob.netlify.app/#projects">portfoliobob.netlify.app</a></sub>

<table>
<tr><td width="50%"><img src="https://portfoliobob.netlify.app/projects/elo/1.webp" alt="Screenshot 1" width="100%"></td><td width="50%"><img src="https://portfoliobob.netlify.app/projects/elo/2.webp" alt="Screenshot 2" width="100%"></td></tr>
<tr><td width="50%"><img src="https://portfoliobob.netlify.app/projects/elo/3.webp" alt="Screenshot 3" width="100%"></td><td width="50%"><img src="https://portfoliobob.netlify.app/projects/elo/4.webp" alt="Screenshot 4" width="100%"></td></tr>
<tr><td width="50%"><img src="https://portfoliobob.netlify.app/projects/elo/5.webp" alt="Screenshot 5" width="100%"></td><td width="50%"><img src="https://portfoliobob.netlify.app/projects/elo/6.webp" alt="Screenshot 6" width="100%"></td></tr>
</table>

## Lokalno pokretanje

U PowerShell-u, iz foldera projekta, pokreni standardnu dev komandu:

```powershell
npm run dev
```

Može i kraće `npm start`, ili dvoklik na `start.cmd`. Skripta pri prvom pokretanju sama pravi `.venv`, instalira pakete i otvara aplikaciju u podrazumevanom browseru. Pri sledećim pokretanjima instalaciju preskače, osim ako se `requirements.txt` promenio.

Pokretanje počinje od porta `5000`. Ako je zauzet, launcher automatski proverava `5001`, `5002` i dalje, a zatim ispisuje i otvara URL sa prvim slobodnim portom.

Postojeći `server/.env` se samo učitava iz aplikacije i launcher ga ne menja.

Za drugi port:

```powershell
npm run dev -- 5001
```

Ako ne želiš automatsko otvaranje browsera:

```powershell
.\start.cmd -NoBrowser
```

Za prinudnu ponovnu instalaciju paketa:

```powershell
.\start.cmd -Install
```

Za zaustavljanje aplikacije pritisni `Ctrl+C`.
