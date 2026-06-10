# Setup — Deploy en GitHub Pages

Pasos para poner el dashboard online en `https://caracas-stocks.github.io/dashboard/`.

Tiempo total estimado: **15 minutos**.

---

## 1. Crear la GitHub Organization (2 min)

1. Andá a https://github.com/account/organizations/new
2. Plan: **Free**
3. Nombre de la organización: `caracas-stocks`
4. Email de contacto: el tuyo
5. "This organization belongs to": *My personal account*
6. **Create organization** → skip los invites de miembros

---

## 2. Crear el repositorio (1 min)

1. Andá a https://github.com/organizations/caracas-stocks/repositories/new
2. Repository name: `dashboard`
3. Visibility: **Public** (necesario para Pages gratis)
4. **NO** marques "Add a README" — lo subimos nosotros
5. **Create repository**

GitHub te va a mostrar comandos para empujar un repo existente. Anotá la URL que aparece (algo como `https://github.com/caracas-stocks/dashboard.git`).

---

## 3. Subir el código (5 min)

Abrí PowerShell o CMD en la carpeta donde está esta guía (`caracas-stocks-dashboard/`):

```powershell
cd "C:\ruta\donde\este\repo"

git init
git add .
git commit -m "Initial commit: BVC dashboard pipeline + first snapshot"
git branch -M main
git remote add origin https://github.com/caracas-stocks/dashboard.git
git push -u origin main
```

Si no tenés git instalado: https://git-scm.com/download/win

Si te pide login: usá tu usuario y un **Personal Access Token** (no la contraseña). Crear uno: https://github.com/settings/tokens/new — marca el scope `repo`.

---

## 4. Activar GitHub Pages (1 min)

1. Andá al repo: https://github.com/caracas-stocks/dashboard
2. **Settings** → **Pages** (menú izquierdo)
3. En "Build and deployment":
   - Source: **GitHub Actions**
4. Guardar.

---

## 5. Permitir que GitHub Actions haga commits (1 min)

1. **Settings** → **Actions** → **General**
2. Bajá hasta "Workflow permissions"
3. Seleccioná: **Read and write permissions**
4. Marcá: **Allow GitHub Actions to create and approve pull requests**
5. **Save**

---

## 6. Disparar el primer run manual (2 min)

1. Andá a la pestaña **Actions** del repo
2. Click en el workflow **"Update BVC Dashboard"** (panel izquierdo)
3. Botón **"Run workflow"** (derecha) → branch `main` → **Run workflow**
4. Esperá ~3-5 min. Cuando el workflow termine en verde, el dashboard estará live.

---

## 7. Verificar (1 min)

Abrí: **https://caracas-stocks.github.io/dashboard/**

Si todavía no carga, esperá 2-3 minutos más (GitHub Pages tarda en propagarse la primera vez).

---

## 8. Apagar el Task Scheduler local (opcional pero recomendado)

Ya no necesitás que tu PC corra el `.bat` cada 15 minutos — GitHub lo hace todo.

```powershell
schtasks /Delete /TN "BVC Update 15min" /F
```

Si querés mantener el local como backup, dejalo corriendo — los dos sistemas son independientes.

---

## Cómo va a funcionar de ahora en adelante

- Cada 15 minutos, L–V, entre 09:00 y 13:15 VET, GitHub Actions:
  1. Corre `bvc.py` → `load_parallel_rates.py` → `build_dashboard.py`
  2. Hace commit del `bvc.db` actualizado + `index.html`
  3. Despliega a Pages

- Cualquiera con la URL puede ver el dashboard, siempre con datos recientes.
- Lo podés disparar manualmente desde Actions → Run workflow.
- El log de cada ejecución queda en Actions → workflow run.

---

## Issues conocidos

**`market.bolsadecaracas.com` devuelve 401.** Los precios same-day del cierre quedan rotos hasta arreglar la auth. La data del día siguiente (vía el API legacy) sí entra bien. Cuando quieras lo investigamos.

**Bloat del repo.** El `bvc.db` (10MB) se commitea en cada run. Después de unas semanas el repo va a pesar varios GB de historia. Cuando llegue ese punto, te paso una migración a GitHub Release artifacts o LFS.

---

## Custom domain (opcional, para después)

Si querés `bvcdashboard.com` o `bolsacaracas.dev` en vez de `caracas-stocks.github.io/dashboard`:

1. Comprá el dominio (Namecheap, Cloudflare, ~$10/año)
2. En el repo: **Settings** → **Pages** → "Custom domain" → escribís tu dominio
3. Agregá un CNAME en tu DNS apuntando a `caracas-stocks.github.io`
4. Esperá ~10 min, listo.
