@echo off
REM Build and launch OcheCore locally with live logs.
setlocal
cd /d "%~dp0.." || exit /b 1

where docker >nul 2>&1
if errorlevel 1 (
    echo Install Docker first. >&2
    exit /b 1
)

docker compose version >nul 2>&1
if errorlevel 1 (
    echo Install the Docker Compose plugin first. >&2
    exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
    echo Start Docker first. >&2
    exit /b 1
)

set "COMPOSE_OVERRIDE="
if exist docker-compose.override.yml set "COMPOSE_OVERRIDE=-f docker-compose.override.yml"

echo OcheCore development: http://localhost:9180 by default; .env can override the port.
echo Stop other Oche containers first. Press Ctrl+C to stop; logs appear below.
docker compose -p oche-dev -f docker-compose.yml %COMPOSE_OVERRIDE% up --build
exit /b %errorlevel%
