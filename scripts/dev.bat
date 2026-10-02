@echo off
REM Build and launch OcheCore locally with live logs.
setlocal
cd /d "%~dp0.." || exit /b 1
if not defined OCHECORE_PUBLISH_HOST set "OCHECORE_PUBLISH_HOST=0.0.0.0"

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

echo OcheCore development: listening on %OCHECORE_PUBLISH_HOST%; .env can override port 9180.
echo Open http://localhost:9180 here or http://YOUR_LAN_IP:9180 from another device.
echo Stop other Oche containers first. Press Ctrl+C to stop; logs appear below.
docker compose -p oche-dev -f docker-compose.yml %COMPOSE_OVERRIDE% up --build
exit /b %errorlevel%
