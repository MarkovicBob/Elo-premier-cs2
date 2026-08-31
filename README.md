# EloPremier

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
