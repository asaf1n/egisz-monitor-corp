"""Flask app: web page to view/edit Firebird + Postgres YAML config."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, Response, jsonify, render_template_string, request

from egisz_monitor_corp.config_loader import (
    default_config_path,
    load_corp_config,
    logical_config_path,
    parse_corp_config_dict,
    save_corp_config,
)
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.fb_client import fetch_firebird_source_peaks
from egisz_monitor_corp.pg_cli_backup import pg_dump_custom_bytes, restore_upload_to_temp_and_run
from egisz_monitor_corp.pg_warehouse import (
    connect_pg,
    fetch_etl_source_peaks_from_pg,
    fetch_healthcheck_snapshot,
    fetch_pg_sync_snapshot,
    test_pg_connection,
)

PAGE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <meta name="color-scheme" content="dark"/>
  <title>FB2PG Sync Configuration</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    /* ~50px tall inputs; shell uses most of viewport width with a capped max for ultra-wide */
    html { -webkit-text-size-adjust: 100%; }
    body { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    .cfg-in {
      box-sizing: border-box;
      min-width: 0;
      max-width: 100%;
      min-height: 3.125rem;
      padding-top: 0.625rem;
      padding-bottom: 0.625rem;
      padding-left: 0.8125rem;
      padding-right: 0.8125rem;
      font-size: 1rem;
      line-height: 1.4;
    }
    @media (min-width: 1024px) {
      .cfg-in { font-size: 0.875rem; }
    }
    @media (max-width: 1023px) {
      .cfg-in { min-height: 3.25rem; padding-top: 0.6875rem; padding-bottom: 0.6875rem; }
    }
    .fixed-scroll::-webkit-scrollbar, pre::-webkit-scrollbar { width: 8px; height: 8px; }
    .fixed-scroll::-webkit-scrollbar-track, pre::-webkit-scrollbar-track { background: #0F1522; }
    .fixed-scroll::-webkit-scrollbar-thumb, pre::-webkit-scrollbar-thumb { background: #2D3F5E; border-radius: 4px; }
    .fixed-scroll::-webkit-scrollbar-thumb:hover, pre::-webkit-scrollbar-thumb:hover { background: #3E5A85; }
    #syncBanner.sync-theme-blue {
      border-color: rgba(80, 158, 227, 0.85);
      background: rgba(12, 74, 110, 0.58);
    }
    #syncBanner.sync-theme-green {
      border-color: rgba(6, 95, 70, 0.8);
      background: rgba(6, 78, 59, 0.42);
    }
    #syncBanner.sync-theme-red {
      border-color: rgba(159, 18, 57, 0.85);
      background: rgba(136, 19, 55, 0.44);
    }
    #syncBanner.sync-theme-orange {
      border-color: rgba(194, 65, 12, 0.9);
      background: rgba(124, 45, 18, 0.5);
    }
    #syncBannerProgressFill {
      background: rgba(34, 211, 238, 0.72);
      border-right: 1px solid rgba(186, 230, 253, 0.9);
    }
    #connStatusProgressFill {
      background: rgba(34, 211, 238, 0.72);
      border-right: 1px solid rgba(186, 230, 253, 0.9);
    }
    #syncBannerProgressFill.sync-progress-indeterminate {
      width: 32% !important;
      animation: sync-bar-indeterminate 1.15s ease-in-out infinite alternate;
    }
    @keyframes sync-bar-indeterminate {
      from { opacity: 0.45; transform: translateX(-15%); }
      to { opacity: 1; transform: translateX(225%); }
    }
    .hc-row-red { color: #FECDD3; }
    .hc-row-yellow { color: #FDE68A; }
    .hc-row-green { color: #A7F3D0; }
    .hc-row { display: flex; align-items: baseline; gap: 0.4rem; min-width: 0; }
    .hc-row .hc-bullet {
      flex-shrink: 0;
      width: 0.55rem;
      height: 0.55rem;
      border-radius: 9999px;
      margin-top: 0.15rem;
    }
    .hc-bullet-red { background: rgba(244, 63, 94, 0.85); box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.3); }
    .hc-bullet-yellow { background: rgba(245, 158, 11, 0.85); box-shadow: 0 0 0 1px rgba(245, 158, 11, 0.3); }
    .hc-bullet-green { background: rgba(16, 185, 129, 0.85); box-shadow: 0 0 0 1px rgba(16, 185, 129, 0.3); }
  </style>
</head>
<body class="min-h-[100dvh] min-h-screen bg-[#121826] text-white lg:h-screen lg:overflow-hidden pb-[env(safe-area-inset-bottom,0px)]">
  <div class="mx-auto flex w-full min-h-[100dvh] min-w-0 max-w-[min(96rem,calc(100vw-1.5rem))] flex-col px-3 py-3 sm:px-[clamp(1rem,4vw,2.5rem)] sm:py-5 lg:min-h-0 lg:h-screen lg:flex-row lg:items-stretch lg:gap-4 lg:py-3">
    <div class="flex min-h-0 min-w-0 flex-1 flex-col lg:min-h-0 lg:overflow-hidden">
    <nav class="mb-3 flex min-h-[2.75rem] shrink-0 items-center justify-center gap-3 text-sm text-[#D1D5DB] lg:text-xs">
      <span class="text-[#509EE3]">FB2PG Sync</span>
      <span class="text-[#4B5563]">|</span>
      <a href="/" class="text-[#4B5563] transition hover:text-[#509EE3]">Обновить страницу</a>
    </nav>

    <section class="flex min-h-0 flex-1 flex-col rounded-xl border border-[#1B2940] bg-[#0F1522] px-4 py-4 shadow-lg sm:px-6 sm:py-5 lg:min-h-0 lg:flex-1 lg:px-8 lg:py-4 lg:flex lg:flex-col">
      <div class="mb-4 shrink-0 border-b border-[#1B2940] pb-3 lg:mb-3 lg:pb-2">
        <h1 class="text-lg font-semibold text-white sm:text-xl lg:text-lg">FB2PG Sync Configuration</h1>
        <p class="mt-1 text-sm leading-relaxed text-[#9CA3AF] sm:text-sm lg:text-xs lg:leading-normal">
          Файл: <code class="text-[#509EE3]">{{ path }}</code><br/>
          Переопределение: переменная окружения <code class="text-[#509EE3]">EGISZ_MONITOR_CONFIG</code> или <code class="text-[#509EE3]">CONFIG_WRITE_PATH</code>.
        </p>
      </div>

      <form id="configForm" class="flex min-h-0 flex-1 flex-col space-y-4 lg:flex lg:min-h-0 lg:flex-1 lg:flex-col lg:gap-3 lg:space-y-0" action="#" onsubmit="return false;">

        <div class="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:gap-x-8 lg:gap-y-0 lg:items-start xl:gap-x-10 lg:shrink-0">
        <!-- Firebird Section -->
        <div class="min-w-0 lg:pr-6 lg:border-r lg:border-[#1B2940]">
          <div class="mb-2">
            <h2 class="text-sm font-medium uppercase tracking-[0.12em] text-[#9CA3AF] lg:text-[11px] lg:font-normal lg:tracking-[0.16em] lg:text-[#4B5563]">Firebird Configuration</h2>
            <p class="mt-1 text-xs leading-relaxed text-[#9CA3AF] sm:text-sm lg:mt-0.5 lg:text-xs lg:leading-snug">TCP к серверу Firebird (как в DBeaver). Если в Docker — используйте <code class="text-white">host.docker.internal</code></p>
          </div>

          <div class="grid grid-cols-1 gap-2.5 md:grid-cols-[minmax(0,1fr)_minmax(5.5rem,6rem)] md:items-end lg:grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(5.5rem,6rem)]">
            <label class="block min-w-0">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">host</span>
              <input name="fb_host" value="{{ fb.host }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full md:w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">port</span>
              <input name="fb_port" type="number" value="{{ fb.port }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid grid-cols-1 gap-2.5 md:grid-cols-[minmax(0,1fr)_minmax(0,10rem)] md:items-end lg:grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(0,10rem)]">
            <label class="block min-w-0">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">database</span>
              <input name="fb_database" value="{{ fb.database }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">charset</span>
              <input name="fb_charset" value="{{ fb.charset }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mx-auto mt-2.5 flex w-full max-w-xl flex-col flex-wrap items-stretch justify-center gap-3 sm:flex-row sm:items-end sm:gap-4">
            <label class="block w-full shrink-0 sm:w-36 sm:flex-none">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">user</span>
              <input name="fb_user" value="{{ fb.user }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full sm:min-w-[12rem] sm:max-w-md sm:flex-1">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">password</span>
              <input name="fb_password" type="password" value="{{ fb.password }}" autocomplete="current-password" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
        </div>

        <!-- PostgreSQL Section -->
        <div class="min-w-0 border-t border-[#1B2940] pt-4 lg:border-0 lg:pl-6 lg:pt-0">
          <div class="mb-2">
            <h2 class="text-sm font-medium uppercase tracking-[0.12em] text-[#9CA3AF] lg:text-[11px] lg:font-normal lg:tracking-[0.16em] lg:text-[#4B5563]">PostgreSQL Configuration</h2>
            <p class="mt-1 text-xs leading-relaxed text-[#9CA3AF] sm:text-sm lg:mt-0.5 lg:text-xs lg:leading-snug">Из пода витрины обычно <code class="text-white">postgres.egisz-monitor.svc.cluster.local:5432</code></p>
          </div>

          <div class="grid grid-cols-1 gap-2.5 md:grid-cols-[minmax(0,1fr)_minmax(5.5rem,6rem)] md:items-end lg:grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(5.5rem,6rem)]">
            <label class="block min-w-0">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">host</span>
              <input name="pg_host" value="{{ pg.host }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">port</span>
              <input name="pg_port" type="number" value="{{ pg.port }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid grid-cols-1 gap-2.5 md:grid-cols-[minmax(0,1fr)_minmax(0,10rem)] md:items-end lg:grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(0,10rem)]">
            <label class="block min-w-0">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">database</span>
              <input name="pg_database" value="{{ pg.database }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">schema</span>
              <input name="pg_schema" value="{{ pg.schema }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid grid-cols-1 gap-2.5 md:grid-cols-[minmax(0,11rem)_minmax(0,1fr)] md:items-end lg:grid-cols-1 xl:grid-cols-[minmax(0,11rem)_minmax(0,1fr)]">
            <label class="block w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">user</span>
              <input name="pg_user" value="{{ pg.user }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block min-w-0">
              <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">password</span>
              <input name="pg_password" type="password" value="{{ pg.password }}" autocomplete="current-password" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
        </div>
        </div>

        <div id="connStatusStrip" class="relative rounded-md border border-transparent bg-transparent min-h-[2.75rem] lg:h-[2.75rem] overflow-hidden text-xs sm:text-sm font-mono transition-[background-color,border-color,color] duration-150" role="status" aria-live="polite">
          <div id="connStatusProgressFill" class="absolute inset-y-0 left-0 top-0 transition-[width] duration-300 ease-out" style="width:0%"></div>
          <div class="relative z-[1] flex min-h-[2.75rem] items-center justify-center gap-3 px-3 py-2 text-center">
            <span id="connStatusText" class="text-inherit leading-snug break-words max-w-[min(100%,64rem)]"></span>
            <span id="connStatusPct" class="hidden shrink-0 font-mono tabular-nums text-inherit min-w-[3rem] text-right"></span>
          </div>
        </div>

        <div class="pt-3 grid grid-cols-1 gap-3 border-t border-[#1B2940] sm:grid-cols-3 sm:gap-3 lg:shrink-0 lg:pt-2">
          <button type="button" id="btnSaveYaml" class="inline-flex min-h-12 min-w-0 w-full items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white sm:min-w-[140px] lg:min-h-[2.875rem]">
            Сохранить в YAML
          </button>
          <button type="button" id="btnTestFb" class="inline-flex min-h-12 min-w-0 w-full items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white sm:min-w-[140px] lg:min-h-[2.875rem]">
            Проверить Firebird
          </button>
          <button type="button" id="btnTestPg" class="inline-flex min-h-12 min-w-0 w-full items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white sm:min-w-[140px] lg:min-h-[2.875rem]">
            Проверить PostgreSQL
          </button>
        </div>

        <div class="mt-3 space-y-3 rounded-lg border border-[#2D3F5E] bg-[#121826] px-3 py-3 sm:px-4">
          <h2 class="text-xs font-medium uppercase tracking-[0.12em] text-[#9CA3AF] lg:text-[10px]">PostgreSQL: бэкап и восстановление</h2>
          <p class="text-xs leading-snug text-[#6B7280]">Клиент в образе: стандартный <code class="text-[#509EE3]">pg_dump</code> / <code class="text-[#509EE3]">pg_restore</code> (пакет <code class="text-[#509EE3]">postgresql-client</code>). Путь на диске задаётся только в диалоге сохранения браузера; на Windows удобно: <code class="text-[#509EE3]">I:\\DB\\egisz-monitor-backups\\</code></p>
          <div class="flex flex-wrap gap-2">
            <button type="button" id="btnPgBackup" class="inline-flex min-h-12 items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-xs text-[#D1D5DB] transition hover:border-[#509EE3] hover:bg-[#223555] hover:text-white">
              Скачать бэкап (-Fc)
            </button>
          </div>
          <p class="text-xs leading-snug text-[#6B7280]">Восстановление: выберите файл <code class="text-[#509EE3]">.dump</code>. Режим фиксированный: <code class="text-[#509EE3]">pg_restore --data-only --no-owner --no-acl</code> (схема в БД уже должна совпадать, например после Job 001+002+005).</p>
          <div class="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-end">
            <label class="block min-w-0 text-xs text-[#9CA3AF]">Файл дампа
              <input type="file" id="pgRestoreFile" accept=".dump,.backup,application/octet-stream" class="mt-1 block w-full max-w-md text-xs text-[#D1D5DB] file:mr-2 file:rounded file:border-0 file:bg-[#1B2940] file:px-2 file:py-1 file:text-[#509EE3]"/>
            </label>
            <button type="button" id="btnPgRestore" class="inline-flex min-h-12 shrink-0 items-center justify-center rounded-md border border-rose-700/70 bg-rose-950/40 px-3.5 py-2.5 font-mono text-xs text-rose-100 transition hover:bg-rose-900/50">
              Восстановить из дампа
            </button>
          </div>
        </div>

        <div class="flex min-h-0 flex-1 flex-col gap-4 border-t border-[#1B2940] pt-4 lg:grid lg:min-h-0 lg:flex-1 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,22rem)_minmax(18rem,1fr)] lg:gap-4 lg:pt-3">
          <div class="flex min-w-0 flex-col gap-3 lg:min-h-0">
            <button type="button" id="btnSync" class="inline-flex w-full min-h-12 items-center justify-center rounded-md border border-[#F59F36] bg-[#F59F36] px-3.5 py-2.5 font-mono text-sm text-[#121826] transition hover:bg-[#FFB95D] lg:min-h-[2.875rem]">
              Запустить синхронизацию
            </button>
            <div class="min-w-0">
              <div class="mb-2">
                <h2 class="text-sm font-medium uppercase tracking-[0.12em] text-[#9CA3AF] lg:text-[11px] lg:font-normal lg:tracking-[0.16em] lg:text-[#4B5563]">ETL Configuration</h2>
              </div>
              <p class="mb-3 max-w-2xl text-sm leading-relaxed text-[#9CA3AF] lg:text-[11px] lg:leading-snug">Полный цикл Firebird → PostgreSQL в фоне. Не закрывайте вкладку до завершения.</p>
              <div class="grid max-w-lg grid-cols-1 gap-3 sm:grid-cols-2 sm:items-end">
                <label class="block w-full max-w-none sm:max-w-[8rem]">
                  <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">batch_size</span>
                  <input name="etl_batch" type="number" value="{{ etl.batch_size }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono tabular-nums text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
                </label>
                <label class="block w-full max-w-none sm:max-w-[10rem]">
                  <span class="font-mono text-xs uppercase tracking-[0.14em] text-[#9CA3AF] lg:text-[11px] lg:tracking-[0.16em] lg:text-[#4B5563]">sync_window_days</span>
                  <input name="etl_sync_days" type="number" value="{{ etl.sync_window_days }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono tabular-nums text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
                </label>
              </div>
              <div class="mt-3 flex min-h-[2.75rem] w-full items-center gap-3">
                <input type="checkbox" name="etl_full_scan" value="1" {{ 'checked' if etl.full_scan else '' }} class="h-5 w-5 shrink-0 rounded border-[#1B2940] bg-[#121826] text-[#509EE3] focus:ring-[#509EE3] focus:ring-offset-[#0F1522]"/>
                <span class="text-sm text-[#9CA3AF] lg:text-xs">Полный скан</span>
              </div>
            </div>
          </div>

          <div class="flex min-h-0 w-full shrink-0 flex-col gap-2 rounded-lg border border-[#2D3F5E] bg-[#121826] px-3 py-3 lg:h-full lg:max-h-none lg:min-h-0 lg:gap-1.5 lg:overflow-y-auto lg:py-2 fixed-scroll">
            <div class="mb-1 shrink-0">
              <h2 class="text-sm font-semibold tracking-tight text-[#D1D5DB] lg:text-sm">Последние значения синхронизации</h2>
            </div>

            <section id="tabSnapshot" class="flex flex-col gap-2.5">
              <div class="space-y-2.5 text-sm leading-relaxed lg:text-sm">
                <div class="flex min-w-0 items-baseline gap-2 py-0.5">
                  <span class="shrink-0 text-xs font-medium text-[#9CA3AF] sm:text-sm lg:text-xs">Дата:</span>
                  <code id="pgSnapSyncAt" class="snap-val min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-sm sm:text-[15px]" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="pgSnapSyncAt" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex min-w-0 items-baseline gap-2 py-0.5">
                  <span class="shrink-0 text-xs font-medium text-[#9CA3AF] sm:text-sm lg:text-xs">LOGID:</span>
                  <code id="pgSnapLogId" class="snap-val min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-sm sm:text-[15px]" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="pgSnapLogId" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex min-w-0 items-baseline gap-2 py-0.5">
                  <span class="shrink-0 text-xs font-medium text-[#9CA3AF] sm:text-sm lg:text-xs">EGMID:</span>
                  <code id="pgSnapEgmid" class="snap-val min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-sm sm:text-[15px]" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="pgSnapEgmid" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex min-w-0 items-baseline gap-2 py-0.5">
                  <span class="shrink-0 text-xs font-medium text-[#9CA3AF] sm:text-sm lg:text-xs">LICENSES.MODIFYDATE:</span>
                  <code id="pgSnapLicMd" class="snap-val min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-sm sm:text-[15px]" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="pgSnapLicMd" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
              </div>
            </section>
          </div>

          <div class="flex min-h-0 min-w-0 flex-1 flex-col rounded-md border border-[#1B2940] bg-[#0B1120] px-3 py-2 text-sm text-[#93A1B6] lg:flex lg:flex-1 lg:flex-col">
            <div class="mb-1 flex min-w-0 items-center justify-between gap-2">
              <div class="text-xs font-medium uppercase tracking-[0.12em] text-[#509EE3] lg:text-[10px] lg:font-normal lg:tracking-[0.16em]">system log</div>
              <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="syncStatus" title="Копировать лог" aria-label="Копировать лог">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
              </button>
            </div>
            <pre id="syncStatus" class="max-h-48 min-h-[2.25rem] flex-1 overflow-y-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-[#D1D5DB] sm:max-h-56 lg:max-h-none lg:text-[11px]"></pre>
          </div>
        </div>
      </form>
    </section>
    </div>

    <aside class="mt-5 flex w-full min-h-0 flex-1 flex-col gap-3 border-t border-[#1B2940] pt-5 lg:mt-0 lg:h-full lg:max-w-[min(22rem,30vw)] lg:w-[22rem] lg:flex-none lg:shrink-0 lg:self-stretch lg:border-l lg:border-t-0 lg:pl-4 lg:pt-0">
      {% if metabase_site_url %}
      <a href="{{ metabase_site_url }}" target="_blank" rel="noopener noreferrer" class="inline-flex min-h-12 w-full shrink-0 items-center justify-center rounded-md border border-[#509EE3]/90 bg-[#1B2940] px-3 py-3 text-center text-sm font-semibold uppercase tracking-[0.12em] text-[#E5F6FF] transition hover:border-[#60B5FF] hover:bg-[#223555] lg:min-h-0 lg:py-2.5 lg:text-[11px] lg:tracking-[0.14em]">
        Metabase →
      </a>
      {% endif %}
      <div class="flex min-h-[14rem] flex-1 flex-col overflow-hidden rounded-lg border border-[#2D3F5E] bg-[#121826] px-3 py-3 lg:min-h-0">
        <h2 class="mb-2 shrink-0 text-sm font-medium uppercase tracking-[0.12em] text-[#9CA3AF] lg:text-[11px] lg:font-normal lg:tracking-[0.16em] lg:text-[#4B5563]">Healthcheck</h2>
        <div class="fixed-scroll flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-0.5 lg:gap-2">
              <p id="hcHint" class="text-xs leading-snug text-[#6B7280] lg:text-[11px]">Сигналы по витрине + топ-3 проблемные клиники + сводка прокси-БД (см. <code class="text-[#509EE3]">v_health_signals</code>, <code class="text-[#509EE3]">v_health_by_clinic</code>).</p>
              <div id="hcLevelSummary" class="flex flex-wrap items-center gap-2 text-xs uppercase tracking-[0.12em] lg:gap-1.5 lg:text-[10px] lg:tracking-[0.14em]">
                <span class="rounded-full px-2 py-0.5 border border-rose-700/60 bg-rose-900/20 text-rose-200" data-level="red">red <span data-count="red">0</span></span>
                <span class="rounded-full px-2 py-0.5 border border-amber-600/60 bg-amber-900/20 text-amber-200" data-level="yellow">yellow <span data-count="yellow">0</span></span>
                <span class="rounded-full px-2 py-0.5 border border-emerald-700/60 bg-emerald-900/20 text-emerald-300" data-level="green">green <span data-count="green">0</span></span>
              </div>
              <div>
                <h3 class="mb-1 text-xs font-medium uppercase tracking-[0.12em] text-[#509EE3] lg:text-[10px] lg:font-normal lg:tracking-[0.14em]">Сигналы</h3>
                <ul id="hcSignals" class="space-y-2 text-sm leading-snug lg:space-y-1.5 lg:text-[11px]"></ul>
              </div>
              <div>
                <h3 class="mb-1 text-xs font-medium uppercase tracking-[0.12em] text-[#509EE3] lg:text-[10px] lg:font-normal lg:tracking-[0.14em]">Топ клиник по проблемам</h3>
                <ul id="hcClinics" class="space-y-2 text-sm leading-snug lg:space-y-1.5 lg:text-[11px]"></ul>
              </div>
              <div>
                <h3 class="mb-1 text-xs font-medium uppercase tracking-[0.12em] text-[#509EE3] lg:text-[10px] lg:font-normal lg:tracking-[0.14em]">Прокси-БД и курсор</h3>
                <ul id="hcProxyDb" class="grid grid-cols-1 gap-x-3 gap-y-2 text-sm leading-snug sm:grid-cols-2 lg:gap-y-1 lg:text-[11px]"></ul>
              </div>
        </div>
      </div>
    </aside>
  </div>

  <script>
  let lastSyncJson = { running: false, error: null, message: '', last_stats: null };
  let lastLiveEgmidFromProgress = null;
  /** Не даём полоске синхронизации откатываться назад при смене фазы (напр. после EGISZ_MESSAGES → EXCHANGELOG). */
  let syncProgressCarry = 0;
  let lastUiMessage = { ok: true, text: '' };
  const STRIP_BASE =
    'relative rounded-md border min-h-[2.75rem] lg:h-[2.75rem] overflow-hidden text-xs sm:text-sm font-mono transition-[background-color,border-color,color] duration-150';
  function rawEtlProgressPercent(p) {
    if (!p || typeof p !== 'object') return null;
    const phase = String(p.phase || '');
    if (phase === 'outbound_fetch' || phase === 'outbound_parse' || phase === 'outbound_postgres' || phase === 'outbound_done') {
      const total = Number(p.outbound_total) || 0;
      const loaded = Number(p.outbound_loaded) || 0;
      if (phase === 'outbound_done' || (phase === 'outbound_postgres' && total === 0)) return 100;
      if (total > 0) return 88 + Math.min(12, Math.round((loaded / total) * 12));
      return 88;
    }
    if (phase === 'enrichment_firebird') return 5;
    if (phase === 'counting' || phase === 'messages_counting') return 12;
    if (phase === 'messages_incremental') {
      const lo = Number(p.loaded_rows) || 0;
      const tot = Number(p.total_rows) || 0;
      if (tot > 0) return Math.min(34, Math.round(12 + (lo / tot) * 22));
      const pg = Number(p.page) || 0;
      if (pg <= 0 && lo <= 0) return 12;
      const loPart = Math.min(12, Math.log10(1 + lo / 80.0) * 4.2);
      const pgPart = Math.min(6, Math.sqrt(pg) * 1.35);
      return Math.min(34, Math.floor(12 + loPart + pgPart));
    }
    if (phase === 'exchangelog_ready') {
      const tr = Number(p.total_rows);
      if (Number.isFinite(tr) && tr === 0) return 88;
      return 36;
    }
    if (phase === 'exchangelog_done') return 100;
    const totalRows = Number(p.total_rows);
    const loadedRows = Number(p.loaded_rows) || 0;
    if (Number.isFinite(totalRows) && totalRows > 0) {
      return Math.min(87, Math.round(36 + (loadedRows / totalRows) * 51));
    }
    if (Number.isFinite(totalRows) && totalRows === 0) {
      return 87;
    }
    return 36;
  }
  function syncProgressPercent(p) {
    const raw = rawEtlProgressPercent(p);
    if (raw == null || !Number.isFinite(raw)) {
      return syncProgressCarry > 0 ? syncProgressCarry : null;
    }
    syncProgressCarry = Math.max(syncProgressCarry, raw);
    return syncProgressCarry;
  }
  function refreshConnStatusStrip() {
    const wrap = document.getElementById('connStatusStrip');
    const textEl = document.getElementById('connStatusText');
    const fill = document.getElementById('connStatusProgressFill');
    const pctEl = document.getElementById('connStatusPct');
    if (!wrap || !textEl || !fill || !pctEl) return;
    const j = lastSyncJson;
    wrap.className = STRIP_BASE;
    textEl.className = 'text-inherit leading-snug break-words max-w-[min(100%,64rem)]';
    fill.style.width = '0%';
    pctEl.className = 'hidden shrink-0 font-mono tabular-nums text-inherit min-w-[3rem] text-right';
    pctEl.textContent = '';
    if (j.running) {
      const p = j.progress;
      const phase = p && typeof p === 'object' ? (p.phase || '') : '';
      const pct = syncProgressPercent(p);
      wrap.classList.add('border-[#509EE3]/85', 'bg-[#0C4A6E]/60', 'text-[#E5F6FF]');
      fill.style.width = pct == null ? '32%' : pct + '%';
      textEl.textContent = 'Синхронизация';
      pctEl.className = 'shrink-0 font-mono tabular-nums text-inherit min-w-[3rem] text-right';
      pctEl.textContent = pct == null ? '…' : pct + '%';
      return;
    }
    if (j.error) {
      wrap.classList.add('border-orange-700/80', 'bg-orange-900/30', 'text-orange-200');
      textEl.textContent = 'Ошибка синхронизации ETL: ' + String(j.error);
      pctEl.className = 'shrink-0 font-mono tabular-nums text-inherit min-w-[3rem] text-right';
      pctEl.textContent = '!';
      return;
    }
    if (lastUiMessage.text) {
      if (lastUiMessage.ok) {
        wrap.classList.add('border-emerald-800/80', 'bg-emerald-900/25', 'text-emerald-400');
      } else {
        wrap.classList.add('border-rose-800/80', 'bg-rose-900/30', 'text-rose-300');
      }
      textEl.textContent = lastUiMessage.text;
      return;
    }
    if (j.last_stats) {
      wrap.classList.add('border-emerald-800/80', 'bg-emerald-900/25', 'text-emerald-400');
      textEl.textContent = (j.message && String(j.message).trim()) ? String(j.message).trim() : 'Синхронизация завершена.';
      return;
    }
    wrap.classList.add('border-transparent', 'bg-transparent', 'text-[#9CA3AF]');
    textEl.textContent = 'Готов к работе';
  }
  const PHASE_RU = {
    enrichment_firebird: 'Справочники: EGISZ_LICENSES (JOIN JPERSONS) из Firebird…',
    messages_incremental: 'Выгрузка EGISZ_MESSAGES из Firebird по курсору EGMID…',
    messages_counting: 'Подсчёт строк EGISZ_MESSAGES в Firebird (для прогресса)…',
    counting: 'Подсчёт строк журнала EXCHANGELOG в Firebird (для прогресса)…',
    exchangelog_ready: 'К журналу EXCHANGELOG: подготовка пагинации',
    exchangelog_export: 'Выгрузка страницы EXCHANGELOG из Firebird (LOGID)…',
    exchangelog_parse: 'Парсинг SOAP/MSGTEXT и сопоставление с EGISZ_MESSAGES по MSGID…',
    parsing: 'Парсинг SOAP/MSGTEXT и обогащение журнала…',
    page_done: 'UPSERT фактов и измерений в PostgreSQL — страница сохранена',
    exchangelog_done: 'Журнал обработан, курсор LOGID обновлён',
    outbound_firebird: 'Исходящие EGISZ_MESSAGES: чтение из Firebird…',
    outbound_fetch: 'Исходящие сообщения (EGISZ_MESSAGES): выборка по окну sync_window_days…',
    outbound_parse: 'Разбор исходящих: дедуп DOCUMENTID, фильтр тестовых клиник…',
    outbound_postgres: 'Исходящие: запись stg_egisz_outbound_documents в PostgreSQL…',
    outbound_done: 'Исходящие: snapshot staging обновлён',
  };
  function buildRunningStatusDetail(j) {
    if (!j.running || !j.progress || typeof j.progress !== 'object') return '';
    const p = j.progress;
    const ph = String(p.phase || '');
    const ru = PHASE_RU[ph] || ph || 'работа';
    if (ph === 'messages_incremental') {
      const bits = [ru];
      bits.push('загружено ' + (Number(p.loaded_rows) || 0) + ' строк');
      const tmsg = Number(p.total_rows);
      if (Number.isFinite(tmsg) && tmsg > 0) bits.push('из ' + tmsg);
      if (Number(p.page) > 0) bits.push('пакет ' + p.page);
      if (p.messages_cursor_egmid !== undefined && p.messages_cursor_egmid !== null)
        bits.push('курсор EGMID ' + p.messages_cursor_egmid);
      return bits.join(' · ');
    }
    if (ph === 'counting' || ph === 'messages_counting') return ru + ' (ожидание ответа Firebird)';
    if (ph === 'enrichment_firebird') return ru;
    if (ph === 'exchangelog_ready' || ph === 'exchangelog_export' || ph === 'exchangelog_parse' || ph === 'parsing' || ph === 'page_done' || ph === 'exchangelog_done') {
      const tr = Number(p.total_rows);
      const lo = Number(p.loaded_rows) || 0;
      const bits = [ru];
      if (Number.isFinite(tr) && tr > 0) bits.push('журнал ' + lo + ' / ' + tr);
      else if (Number.isFinite(tr) && tr === 0) bits.push('журнал: новых строк нет');
      return bits.join(' · ');
    }
    if (ph === 'outbound_firebird' || ph === 'outbound_fetch' || ph === 'outbound_parse' || ph === 'outbound_postgres' || ph === 'outbound_done') {
      const bits = [ru];
      const otot = Number(p.outbound_total);
      const olo = Number(p.outbound_loaded) || 0;
      if (Number.isFinite(otot) && otot > 0) bits.push('исходящие ' + olo + ' / ' + otot);
      return bits.join(' · ');
    }
    return ru;
  }
  const SYNC_BANNER_BASE = 'relative mt-2 lg:mt-0 overflow-hidden rounded-lg border transition-[border-color,background-color] duration-200 lg:shrink-0 ';
  const SYNC_FILL_CURRENT = 'absolute inset-y-0 left-0 top-0 rounded-l-lg transition-[width] duration-300 ease-out';
  const SYNC_TITLE_BLUE = 'text-[11px] uppercase tracking-[0.16em] font-semibold text-[#509EE3]';
  const SYNC_TITLE_GREEN = 'text-[11px] uppercase tracking-[0.16em] font-semibold text-emerald-200';
  const SYNC_TITLE_RED = 'text-[11px] uppercase tracking-[0.16em] font-semibold text-rose-100';
  const SYNC_TITLE_ORANGE = 'text-[11px] uppercase tracking-[0.16em] font-semibold text-orange-100';
  const SYNC_META_BASE = 'mt-2 lg:mt-0 whitespace-pre-line font-mono text-[10px] leading-relaxed min-h-[2.25rem] lg:h-[4.75rem] lg:overflow-y-auto fixed-scroll transition-colors duration-200 ';
  function setProgressTheme(banner, theme) {
    if (!banner) return;
    banner.className = SYNC_BANNER_BASE + 'sync-theme-' + theme;
  }
  function setBarIndeterminate(bar, on) {
    if (!bar) return;
    if (on) bar.classList.add('sync-progress-indeterminate');
    else bar.classList.remove('sync-progress-indeterminate');
  }
  function renderProgress(j) {
    const banner = document.getElementById('syncBanner');
    const titleEl = document.getElementById('syncBannerTitle');
    const fill = document.getElementById('syncBannerProgressFill');
    const pctEl = document.getElementById('syncBannerPct');
    const meta = document.getElementById('syncProgressMeta');
    const p = j.progress;

    if (!banner || !titleEl || !fill || !pctEl || !meta) return;
    fill.className = SYNC_FILL_CURRENT;

    if (!j.running && j.error) {
      setProgressTheme(banner, 'orange');
      titleEl.className = SYNC_TITLE_ORANGE;
      titleEl.textContent = 'Ошибка';
      fill.style.width = '100%';
      setBarIndeterminate(fill, false);
      pctEl.textContent = '!';
      const errLine = j.error ? String(j.error) : '';
      const msgLine = (j.message && String(j.message).trim()) ? String(j.message).trim() : '';
      let metaBody = '';
      if (msgLine && errLine && msgLine.indexOf(errLine) >= 0) metaBody = msgLine;
      else if (msgLine && errLine) metaBody = msgLine + String.fromCharCode(10) + errLine;
      else metaBody = msgLine || errLine || 'Синхронизация завершилась с ошибкой.';
      meta.textContent = metaBody;
      meta.className = SYNC_META_BASE + 'text-orange-200/95';
      return;
    }

    if (!j.running) {
      if (j.last_stats) {
        setProgressTheme(banner, 'green');
        titleEl.className = SYNC_TITLE_GREEN;
        titleEl.textContent = 'Готово';
        fill.style.width = '100%';
        setBarIndeterminate(fill, false);
        pctEl.textContent = '100%';
        meta.textContent = (j.message && String(j.message).trim()) ? String(j.message).trim() : 'Синхронизация завершена.';
        meta.className = SYNC_META_BASE + 'text-emerald-100';
        return;
      }
      setProgressTheme(banner, 'blue');
      titleEl.className = SYNC_TITLE_BLUE;
      titleEl.textContent = 'Синхронизация';
      fill.style.width = '0%';
      setBarIndeterminate(fill, false);
      pctEl.textContent = '';
      meta.textContent = '';
      meta.className = SYNC_META_BASE + 'text-[#9CA3AF]';
      return;
    }

    setProgressTheme(banner, 'blue');
    titleEl.className = SYNC_TITLE_BLUE;
    titleEl.textContent = 'Синхронизация';
    meta.className = SYNC_META_BASE + 'text-[#9CA3AF]';

    if (!p || typeof p !== 'object') {
      setProgressTheme(banner, 'orange');
      titleEl.className = SYNC_TITLE_ORANGE;
      fill.style.width = '';
      setBarIndeterminate(fill, true);
      pctEl.textContent = '…';
      meta.textContent =
        'ПОДГОТОВКА' +
        String.fromCharCode(10) +
        'Запуск пайплайна: конфигурация, курсор ETL, справочники из Firebird…' +
        String.fromCharCode(10) +
        'Детальный прогресс появится после подсчёта объёма журнала.';
      return;
    }

    const phase = p.phase || '';
    const phaseTitle = (PHASE_RU[phase] || phase || 'прогресс').toUpperCase();
    const barPct = syncProgressPercent(p);

    function totalRowsState(pp) {
      if (!Object.prototype.hasOwnProperty.call(pp, 'total_rows') || pp.total_rows === null || pp.total_rows === undefined) {
        return { kind: 'absent' };
      }
      const n = Number(pp.total_rows);
      if (!Number.isFinite(n)) return { kind: 'absent' };
      if (n > 0) return { kind: 'positive', n };
      return { kind: 'zero' };
    }

    function journalFacts(pp) {
      const jf = Number(pp.journal_facts);
      if (Number.isFinite(jf) && jf >= 0) return jf;
      return Number(pp.parsed_facts) || 0;
    }

    if (phase === 'enrichment_firebird') {
      setProgressTheme(banner, 'blue');
      titleEl.className = SYNC_TITLE_BLUE;
      setBarIndeterminate(fill, false);
      const ep = barPct != null ? barPct : 8;
      fill.style.width = ep + '%';
      pctEl.textContent = ep + '%';
      meta.className = SYNC_META_BASE + 'text-[#9CA3AF]';
      meta.textContent =
        phaseTitle +
        String.fromCharCode(10) +
        'Загрузка EGISZ_LICENSES с полями JPERSONS (один запрос к Firebird)…';
      return;
    }

    if (phase === 'messages_incremental') {
      setProgressTheme(banner, 'blue');
      titleEl.className = SYNC_TITLE_BLUE;
      setBarIndeterminate(fill, false);
      const lo = Number(p.loaded_rows) || 0;
      const tot = Number(p.total_rows) || 0;
      const pg = Number(p.page) || 0;
      let mpct = 12;
      if (tot > 0) {
        mpct = Math.min(34, Math.round(12 + (lo / tot) * 22));
      } else if (pg <= 0 && lo <= 0) {
        mpct = 12;
      } else {
        const loPart = Math.min(12, Math.log10(1 + lo / 80.0) * 4.2);
        const pgPart = Math.min(6, Math.sqrt(pg) * 1.35);
        mpct = Math.min(34, Math.floor(12 + loPart + pgPart));
      }
      const disp = barPct != null ? barPct : mpct;
      fill.style.width = disp + '%';
      pctEl.textContent = disp + '%';
      meta.className = SYNC_META_BASE + 'text-[#9CA3AF]';
      const egm =
        p.messages_cursor_egmid !== undefined && p.messages_cursor_egmid !== null
          ? String(p.messages_cursor_egmid)
          : '—';
      meta.textContent =
        phaseTitle +
        String.fromCharCode(10) +
        'Строк загружено: ' +
        lo +
        (tot > 0 ? ' / ' + tot : '') +
        (pg > 0 ? ' · пакет ' + pg : '') +
        String.fromCharCode(10) +
        'Курсор EGMID: ' +
        egm;
      return;
    }

    if (phase === 'outbound_firebird') {
      setProgressTheme(banner, 'orange');
      titleEl.className = SYNC_TITLE_ORANGE;
      fill.style.width = '';
      setBarIndeterminate(fill, true);
      pctEl.textContent = '…';
      const tr = totalRowsState(p);
      const jlo = Number(p.loaded_rows) || 0;
      let lineA = 'Чтение EGISZ_MESSAGES из Firebird (окно sync_window_days). Пока идёт запрос, факты журнала уже в витрине.';
      let lineB = 'Фактов в fact_egisz_transactions: ' + journalFacts(p) + ' · staging ошибок (журнал): ' + (Number(p.staging_errors) || 0);
      if (tr.kind === 'positive') lineB += ' · журнал: ' + jlo + ' / ' + tr.n + ' строк';
      else if (tr.kind === 'zero') lineB += ' · журнал: новых строк не было';
      meta.textContent = phaseTitle + String.fromCharCode(10) + lineA + String.fromCharCode(10) + lineB;
      return;
    }

    setBarIndeterminate(fill, false);

    let pct = 0;
    let lineA = '';
    let lineB = '';

    if (phase === 'outbound_fetch' || phase === 'outbound_parse' || phase === 'outbound_postgres' || phase === 'outbound_done') {
      const otot = Number(p.outbound_total) || 0;
      const olo = Number(p.outbound_loaded) || 0;
      if (phase === 'outbound_postgres' && otot === 0) {
        pct = 100;
        lineA = 'Запись stg_egisz_outbound_documents: нет строк для вставки.';
      } else if (otot > 0) {
        pct = Math.min(100, Math.round((olo / otot) * 100));
        if (phase === 'outbound_fetch') lineA = 'Исходящие: выборка из Firebird, подготовка списка…';
        else if (phase === 'outbound_parse') lineA = 'Исходящие: разбор ' + olo + ' / ' + otot + ' строк выборки';
        else if (phase === 'outbound_postgres') lineA = 'Исходящие: запись staging в PostgreSQL…';
        else lineA = 'Исходящие: staging обновлён (' + olo + ' строк).';
      } else {
        if (phase === 'outbound_done') {
          pct = 100;
          lineA = 'Исходящие: обновление staging завершено (строк для вставки не было).';
        } else {
          pct = 0;
          lineA = 'Исходящие: в окне нет строк EGISZ_MESSAGES.';
        }
      }
      const tr = totalRowsState(p);
      const jlo = Number(p.loaded_rows) || 0;
      const stgOut = Number(p.parsed_facts) || 0;
      lineB =
        'Фактов в витрине (журнал): ' +
        journalFacts(p) +
        ' · staging исходящих (строк): ' +
        stgOut +
        ' · staging ошибок (журнал): ' +
        (Number(p.staging_errors) || 0);
      if (tr.kind === 'positive') lineB += ' · журнал EXCHANGELOG: ' + jlo + ' / ' + tr.n + ' строк';
      else if (tr.kind === 'zero') lineB += ' · журнал: 0 строк к загрузке';
    } else {
      const tr = totalRowsState(p);
      const lo = Number(p.loaded_rows) || 0;

      if (phase === 'counting') {
        lineA =
          'Выполняется подсчёт строк EXCHANGELOG в Firebird (на большой базе это может занять время)…';
        pct = 12;
      } else if (phase === 'messages_counting') {
        lineA =
          'Выполняется подсчёт строк EGISZ_MESSAGES в окне sync_window_days (на большой базе это может занять время)…';
        pct = 12;
      } else if (tr.kind === 'positive') {
        pct = Math.min(87, Math.round(36 + (lo / tr.n) * 51));
        lineA = 'Загружено из журнала: ' + lo + ' / ' + tr.n + ' строк';
      } else if (tr.kind === 'zero') {
        pct = phase === 'exchangelog_done' ? 100 : 87;
        if (phase === 'exchangelog_ready' || phase === 'exchangelog_export' || phase === 'exchangelog_parse' || phase === 'parsing') {
          lineA = 'Новых строк журнала нет (0 к обработке по курсору LOGID).';
        } else {
          lineA = 'Строк журнала к загрузке: 0.';
        }
      } else {
        lineA = 'Объём журнала пока не передан сервером (редкий переход между фазами).';
        pct = 36;
      }

      lineB =
        'Фактов в витрине (после парсинга журнала): ' +
        journalFacts(p) +
        ' · staging ошибок: ' +
        (Number(p.staging_errors) || 0);
      if (p.page) lineB += ' · страница ' + p.page;
    }

    const fillPct = barPct != null ? barPct : pct;
    fill.style.width = fillPct + '%';
    pctEl.textContent = fillPct + '%';
    meta.textContent = phaseTitle + String.fromCharCode(10) + lineA + String.fromCharCode(10) + lineB;
  }
  const PG_SNAP_MONTHS_RU = [
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
  ];
  function formatPgSyncAtRu(isoStr) {
    if (!isoStr) return '—';
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return isoStr;
    const day = d.getUTCDate();
    const mon = PG_SNAP_MONTHS_RU[d.getUTCMonth()];
    const hh = String(d.getUTCHours()).padStart(2, '0');
    const mm = String(d.getUTCMinutes()).padStart(2, '0');
    return day + ' ' + mon + ' ' + hh + ':' + mm;
  }
  function applySnapshotLiveEgmidOverlay() {
    const egEl = document.getElementById('pgSnapEgmid');
    if (!egEl) return;
    const sj = lastSyncJson;
    if (!sj || !sj.running) {
      lastLiveEgmidFromProgress = null;
      return;
    }
    const p = sj.progress;
    if (p && typeof p === 'object' && p.messages_cursor_egmid != null && p.messages_cursor_egmid !== '') {
      lastLiveEgmidFromProgress = p.messages_cursor_egmid;
    }
    if (lastLiveEgmidFromProgress == null) return;
    const raw = String(lastLiveEgmidFromProgress);
    const n = Number(raw);
    const disp = Number.isFinite(n) ? n.toLocaleString('ru-RU') : raw;
    egEl.textContent = disp;
    egEl.setAttribute('data-raw', raw);
    egEl.title = raw;
  }
  async function pollPgSnapshot() {
    const syncEl = document.getElementById('pgSnapSyncAt');
    const logEl = document.getElementById('pgSnapLogId');
    const egEl = document.getElementById('pgSnapEgmid');
    const licEl = document.getElementById('pgSnapLicMd');
    if (!syncEl || !logEl || !egEl || !licEl) return;
    function clearSnap() {
      syncEl.textContent = '—';
      syncEl.setAttribute('data-raw', '');
      syncEl.title = '';
      logEl.textContent = '—';
      logEl.setAttribute('data-raw', '');
      logEl.title = '';
      egEl.textContent = '—';
      egEl.setAttribute('data-raw', '');
      egEl.title = '';
      licEl.textContent = '—';
      licEl.setAttribute('data-raw', '');
      licEl.title = '';
    }
    try {
      const r = await fetch('/api/pg/sync-snapshot', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      POLL_FAIL.snap = 0;
      if (!j.ok) {
        clearSnap();
        applySnapshotLiveEgmidOverlay();
        return;
      }
      const ds = j.sync_at != null && String(j.sync_at).trim() !== '' ? String(j.sync_at) : null;
      if (ds) {
        syncEl.textContent = formatPgSyncAtRu(ds);
        syncEl.setAttribute('data-raw', ds);
        syncEl.title = ds;
      } else {
        syncEl.textContent = '—';
        syncEl.setAttribute('data-raw', '');
        syncEl.title = '';
      }
      const logStr = j.log_id != null ? String(j.log_id) : null;
      logEl.textContent = logStr || '—';
      logEl.setAttribute('data-raw', logStr || '');
      logEl.title = logStr || '';
      const egPrimary = (j.egmid != null) ? j.egmid : j.egmid_staging_max;
      const egStr = egPrimary != null ? String(egPrimary) : null;
      egEl.textContent = egStr || '—';
      egEl.setAttribute('data-raw', egStr || '');
      egEl.title = egStr || '';
      const licRaw = j.licenses_modifydate != null && String(j.licenses_modifydate).trim() !== '' ? String(j.licenses_modifydate) : null;
      if (licRaw) {
        const d = new Date(licRaw);
        const disp = Number.isNaN(d.getTime()) ? licRaw : formatPgSyncAtRu(licRaw);
        licEl.textContent = disp;
        licEl.setAttribute('data-raw', licRaw);
        licEl.title = licRaw;
      } else {
        licEl.textContent = '—';
        licEl.setAttribute('data-raw', '');
        licEl.title = '';
      }
      applySnapshotLiveEgmidOverlay();
    } catch (e) {
      POLL_FAIL.snap += 1;
      if (POLL_FAIL.snap <= 2) return;
      clearSnap();
      applySnapshotLiveEgmidOverlay();
    }
  }
  function fmtPctOrNum(value, unit) {
    if (value == null || Number.isNaN(Number(value))) return '—';
    if (unit === '%') return Number(value).toFixed(1) + '%';
    if (unit === 'sec_since_update') {
      const sec = Math.max(0, Math.round(Number(value)));
      if (sec < 60) return sec + ' c';
      if (sec < 3600) return Math.round(sec / 60) + ' мин';
      return Math.round(sec / 3600) + ' ч';
    }
    return String(value);
  }
  function fmtNum(value) {
    if (value == null || Number.isNaN(Number(value))) return '—';
    return Number(value).toLocaleString('ru-RU');
  }
  function fmtIsoShort(s) {
    if (!s) return '—';
    return formatPgSyncAtRu(s);
  }
  async function pollHealthcheck() {
    const hint = document.getElementById('hcHint');
    const summary = document.getElementById('hcLevelSummary');
    const signalsList = document.getElementById('hcSignals');
    const clinicsList = document.getElementById('hcClinics');
    const proxyList = document.getElementById('hcProxyDb');
    if (!hint || !summary || !signalsList || !clinicsList || !proxyList) return;
    try {
      const r = await fetch('/api/healthcheck', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      POLL_FAIL.hc = 0;
      const lvl = (j.level_summary && typeof j.level_summary === 'object') ? j.level_summary : { red: 0, yellow: 0, green: 0 };
      summary.querySelectorAll('[data-count]').forEach(function (el) {
        const k = el.getAttribute('data-count');
        el.textContent = String(lvl[k] || 0);
      });
      hint.textContent = (j.errors && j.errors.length)
        ? 'Ошибки healthcheck: ' + j.errors.join(' · ')
        : 'Источник: v_health_signals + v_health_by_clinic + v_health_proxy_db. Снимок ' + (j.generated_at ? formatPgSyncAtRu(j.generated_at) : '—') + '.';
      hint.classList.toggle('text-orange-300', !!(j.errors && j.errors.length));

      signalsList.innerHTML = '';
      const sigs = Array.isArray(j.signals) ? j.signals : [];
      if (!sigs.length) {
        signalsList.innerHTML = '<li class="text-[#6B7280]">Нет сигналов (витрина не наполнена или схема не применена).</li>';
      } else {
        for (const s of sigs) {
          const li = document.createElement('li');
          const lvl2 = (s.level || 'green').toLowerCase();
          li.className = 'hc-row hc-row-' + lvl2;
          const bullet = document.createElement('span');
          bullet.className = 'hc-bullet hc-bullet-' + lvl2;
          li.appendChild(bullet);
          const body = document.createElement('div');
          body.className = 'min-w-0 flex-1';
          const title = document.createElement('div');
          title.className = 'flex items-baseline gap-2 min-w-0';
          const name = document.createElement('span');
          name.className = 'truncate';
          name.textContent = s.title || s.code;
          const val = document.createElement('span');
          val.className = 'shrink-0 font-mono text-[10px] text-[#9CA3AF]';
          val.textContent = fmtPctOrNum(s.value, s.value_unit);
          title.appendChild(name);
          title.appendChild(val);
          body.appendChild(title);
          if (s.hint) {
            const hintEl = document.createElement('div');
            hintEl.className = 'text-[10px] text-[#6B7280]';
            hintEl.textContent = s.hint;
            body.appendChild(hintEl);
          }
          li.appendChild(body);
          signalsList.appendChild(li);
        }
      }

      clinicsList.innerHTML = '';
      const clinics = Array.isArray(j.by_clinic_top) ? j.by_clinic_top : [];
      if (!clinics.length) {
        clinicsList.innerHTML = '<li class="text-[#6B7280]">Нет агрегатов по клиникам.</li>';
      } else {
        for (const c of clinics) {
          const li = document.createElement('li');
          const lvl2 = (c.health_level || 'green').toLowerCase();
          li.className = 'hc-row hc-row-' + lvl2;
          const bullet = document.createElement('span');
          bullet.className = 'hc-bullet hc-bullet-' + lvl2;
          li.appendChild(bullet);
          const body = document.createElement('div');
          body.className = 'min-w-0 flex-1';
          const top = document.createElement('div');
          top.className = 'flex items-baseline gap-2 min-w-0';
          const name = document.createElement('span');
          name.className = 'truncate';
          name.textContent = c.clinic_name || ('JID ' + c.jid);
          const er = document.createElement('span');
          er.className = 'shrink-0 font-mono text-[10px] text-[#9CA3AF]';
          er.textContent = (c.error_rate_24h != null ? Number(c.error_rate_24h).toFixed(1) + '%' : '—') + ' err';
          top.appendChild(name);
          top.appendChild(er);
          body.appendChild(top);
          const meta = document.createElement('div');
          meta.className = 'text-[10px] text-[#6B7280]';
          meta.textContent = '24ч: ' + fmtNum(c.facts_24h) + ' · queue: ' + fmtNum(c.pending_now) + ' · last ' + fmtIsoShort(c.last_seen_at);
          body.appendChild(meta);
          li.appendChild(body);
          clinicsList.appendChild(li);
        }
      }

      proxyList.innerHTML = '';
      const p = j.proxy_db || {};
      const items = [
        { label: 'Staging всего', value: fmtNum(p.stg_outbound_total) },
        { label: 'Без EGMID', value: fmtNum(p.stg_without_egmid) },
        { label: 'Очередь > 24ч', value: fmtNum(p.pending_older_24h) },
        { label: 'Очередь 1–24ч', value: fmtNum(p.pending_1_24h) },
        { label: 'Staging max EGMID', value: fmtNum(p.staging_max_egmid) },
        { label: 'Firebird max EGMID', value: fmtNum(p.fb_max_egmid) },
        { label: 'EGMID lag', value: fmtNum(p.egmid_lag) },
        { label: 'last_log_id', value: fmtNum(p.etl_last_log_id) },
      ];
      for (const it of items) {
        const li = document.createElement('li');
        li.className = 'flex items-baseline justify-between gap-1 min-w-0';
        const lab = document.createElement('span');
        lab.className = 'truncate text-[#9CA3AF] text-[10px]';
        lab.textContent = it.label;
        const v = document.createElement('span');
        v.className = 'shrink-0 font-mono text-[10px] text-[#E5E7EB]';
        v.textContent = it.value;
        li.appendChild(lab);
        li.appendChild(v);
        proxyList.appendChild(li);
      }
    } catch (e) {
      POLL_FAIL.hc += 1;
      if (POLL_FAIL.hc <= 2) return;
      const transport = isFetchTransportError(e);
      hint.textContent = transport
        ? 'Healthcheck временно недоступен (conf-ui перезапускается).'
        : 'Ошибка опроса /api/healthcheck: ' + (e && e.message ? e.message : e);
      hint.classList.add('text-orange-300');
    }
  }

  document.querySelectorAll('.pg-snap-copy').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const id = btn.getAttribute('data-copy');
      const el = id ? document.getElementById(id) : null;
      const rawAttr = el && el.getAttribute ? el.getAttribute('data-raw') : null;
      const raw = (rawAttr != null && rawAttr !== '') ? rawAttr : (el && el.textContent ? el.textContent.trim() : '');
      if (!raw || raw === '—') return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(raw).catch(function () {});
      } else {
        const ta = document.createElement('textarea');
        ta.value = raw;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (e2) {}
        document.body.removeChild(ta);
      }
    });
  });
  // Счётчики последовательных сбоев fetch: сценарий «conf-ui pod пересоздаётся, port-forward
  // разорван» теперь сначала молчит, потом показывает аккуратную диагностику без визуального шума.
  const POLL_FAIL = { sync: 0, snap: 0, hc: 0 };
  function isFetchTransportError(e) {
    if (!e) return false;
    const name = e && e.name ? String(e.name) : '';
    const msg = e && e.message ? String(e.message) : String(e);
    return name === 'TypeError' && /fetch|network|load failed/i.test(msg);
  }
  async function pollSync() {
    const el = document.getElementById('syncStatus');
    try {
      const r = await fetch('/api/sync/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      POLL_FAIL.sync = 0;
      const wasRunning = lastSyncJson && lastSyncJson.running;
      if (!j.running) {
        syncProgressCarry = 0;
      } else if (!wasRunning) {
        syncProgressCarry = 0;
      }
      lastSyncJson = j;
      renderProgress(j);
      refreshConnStatusStrip();
      const parts = [];
      const msg = j.message != null ? String(j.message).trim() : '';
      if (msg) parts.push(msg);
      if (j.running) {
        const det = buildRunningStatusDetail(j);
        parts.push('Статус: ' + (det || 'выполняется'));
      } else if (j.error) {
        parts.push('Статус: ошибка');
      } else if (j.last_stats) {
        parts.push('Статус: выполнено');
      } else {
        parts.push('Статус: ожидание');
      }
      if (j.error) {
        const errStr = String(j.error);
        const already = msg && msg.indexOf(errStr) >= 0;
        if (!already) parts.push('Ошибка: ' + errStr);
      }
      if (j.last_stats) parts.push(JSON.stringify(j.last_stats, null, 2));
      el.textContent = parts.filter(Boolean).join(String.fromCharCode(10));
      applySnapshotLiveEgmidOverlay();
    } catch (e) {
      POLL_FAIL.sync += 1;
      // Один-два пропущенных тика во время rollout/port-forward — нормальное явление, не шумим.
      if (POLL_FAIL.sync <= 2) return;
      const transport = isFetchTransportError(e);
      const reason = transport
        ? 'conf-ui недоступен (rollout / port-forward / sleep). Повтор через несколько секунд.'
        : ('Ошибка опроса: ' + (e && e.message ? e.message : e));
      el.textContent = reason;
    }
  }
  function showCfgMessage(ok, text) {
    lastUiMessage = { ok: !!ok, text: text || '' };
    refreshConnStatusStrip();
  }
  async function postConfigForm(url) {
    const fd = new FormData(document.getElementById('configForm'));
    const r = await fetch(url, {
      method: 'POST',
      body: fd,
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    const raw = await r.text();
    let j;
    try {
      j = JSON.parse(raw);
    } catch (e) {
      showCfgMessage(false, 'Ответ сервера не JSON (код ' + r.status + '). ' + raw.slice(0, 400));
      return;
    }
    showCfgMessage(!!j.ok, j.message || (j.ok ? 'OK' : 'Ошибка'));
  }
  document.getElementById('btnSaveYaml').onclick = async function () {
    if (!confirm('Сохранить текущую конфигурацию из полей формы в YAML на сервере?')) return;
    try {
      await postConfigForm('/save');
    } catch (e) {
      showCfgMessage(false, String(e));
    }
  };
  document.getElementById('btnTestFb').onclick = async function () {
    try {
      await postConfigForm('/test-fb');
    } catch (e) {
      showCfgMessage(false, String(e));
    }
  };
  document.getElementById('btnTestPg').onclick = async function () {
    try {
      await postConfigForm('/test-pg');
    } catch (e) {
      showCfgMessage(false, String(e));
    }
  };
  document.getElementById('btnPgBackup').onclick = async function () {
    const fd = new FormData(document.getElementById('configForm'));
    let r;
    try {
      r = await fetch('/api/pg/backup', {
        method: 'POST',
        body: fd,
        credentials: 'same-origin',
      });
    } catch (e) {
      showCfgMessage(false, String(e));
      return;
    }
    if (!r.ok) {
      const raw = await r.text();
      let j;
      try {
        j = JSON.parse(raw);
      } catch (e2) {
        showCfgMessage(false, 'Бэкап: код ' + r.status + '. ' + raw.slice(0, 400));
        return;
      }
      showCfgMessage(false, (j && j.message) || raw.slice(0, 400));
      return;
    }
    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    let name = 'egisz_pg.dump';
    const m = cd.match(/filename="([^"]+)"/i);
    if (m && m[1]) {
      try {
        name = decodeURIComponent(m[1].replace(/\"/g, '').trim());
      } catch (e3) {
        name = m[1].replace(/\"/g, '').trim() || name;
      }
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showCfgMessage(true, 'Файл бэкапа скачан (сохраните в нужную папку, например I:\\\\DB\\\\egisz-monitor-backups\\\\).');
  };
  document.getElementById('btnPgRestore').onclick = async function () {
    const inp = document.getElementById('pgRestoreFile');
    if (!inp.files || !inp.files[0]) {
      showCfgMessage(false, 'Выберите файл дампа.');
      return;
    }
    if (!confirm('ВНИМАНИЕ: pg_restore перезапишет данные в целевой БД (data-only). Продолжить?')) return;
    const fd = new FormData(document.getElementById('configForm'));
    fd.append('dump', inp.files[0], inp.files[0].name || 'backup.dump');
    let r;
    try {
      r = await fetch('/api/pg/restore', {
        method: 'POST',
        body: fd,
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
    } catch (e) {
      showCfgMessage(false, String(e));
      return;
    }
    const raw = await r.text();
    let j;
    try {
      j = JSON.parse(raw);
    } catch (e2) {
      showCfgMessage(false, 'Ответ сервера не JSON (код ' + r.status + '). ' + raw.slice(0, 400));
      return;
    }
    showCfgMessage(!!j.ok, j.message || (j.ok ? 'OK' : 'Ошибка'));
  };
  document.getElementById('btnSync').onclick = async function() {
    const el = document.getElementById('syncStatus');
    el.textContent = 'Запрос...';
    const fd = new FormData(document.getElementById('configForm'));
    let r;
    try {
      r = await fetch('/api/sync/start', {
        method: 'POST',
        body: fd,
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
    } catch (e) {
      el.textContent = String(e);
      return;
    }
    const raw = await r.text();
    let j;
    try {
      j = JSON.parse(raw);
    } catch (e2) {
      el.textContent = 'Ответ сервера не JSON (код ' + r.status + '). ' + raw.slice(0, 400);
      return;
    }
    el.textContent = j.message || j.error || JSON.stringify(j);
    await pollSync();
  };
  // Опрос только пока вкладка видима. Скрытая вкладка через 5–10 минут sleep'а
  // получала залп fetch'ей разом и стабильно ловила TypeError: Failed to fetch.
  let fastTimer = null;
  let slowTimer = null;
  function startPolling() {
    if (fastTimer == null) {
      fastTimer = setInterval(function () {
        pollSync();
        pollPgSnapshot();
      }, 1500);
    }
    if (slowTimer == null) {
      slowTimer = setInterval(function () {
        pollHealthcheck();
      }, 30000);
    }
  }
  function stopPolling() {
    if (fastTimer != null) { clearInterval(fastTimer); fastTimer = null; }
    if (slowTimer != null) { clearInterval(slowTimer); slowTimer = null; }
  }
  startPolling();
  pollSync();
  refreshConnStatusStrip();
  setTimeout(function () { pollPgSnapshot(); }, 40);
  setTimeout(function () { pollHealthcheck(); }, 120);
  window.addEventListener('pageshow', function () {
    pollSync();
    refreshConnStatusStrip();
    setTimeout(function () { pollPgSnapshot(); }, 40);
    setTimeout(function () { pollHealthcheck(); }, 120);
  });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      startPolling();
      pollSync();
      setTimeout(function () { pollPgSnapshot(); }, 40);
      setTimeout(function () { pollHealthcheck(); }, 120);
    } else {
      stopPolling();
    }
  });
  </script>
</body>
</html>
"""


def _merged_yaml_dict_from_form(p: Path, form: Mapping[str, Any]) -> dict[str, Any]:
    """Merge POSTed form fields into the YAML root dict on disk (passwords unchanged if left blank)."""
    import yaml

    old: dict[str, Any] = {}
    if p.is_file():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            old = loaded
    old.setdefault("firebird", {})
    old.setdefault("postgres", {})
    old.setdefault("etl", {})
    old.setdefault("metabase", old.get("metabase") or {})

    fb_updates: dict[str, object] = {
        "host": str(form.get("fb_host", "") or "").strip(),
        "port": int(form.get("fb_port") or 3050),
        "database": str(form.get("fb_database", "") or "").strip(),
        "user": str(form.get("fb_user", "") or "").strip(),
        "charset": str(form.get("fb_charset", "WIN1251") or "").strip() or "WIN1251",
    }
    if form.get("fb_password"):
        fb_updates["password"] = str(form.get("fb_password", "") or "")
    old["firebird"].update(fb_updates)

    pg_updates: dict[str, object] = {
        "host": str(form.get("pg_host", "") or "").strip(),
        "port": int(form.get("pg_port") or 5432),
        "database": str(form.get("pg_database", "") or "").strip(),
        "user": str(form.get("pg_user", "") or "").strip(),
        "schema": str(form.get("pg_schema", "public") or "").strip() or "public",
    }
    if form.get("pg_password"):
        pg_updates["password"] = str(form.get("pg_password", "") or "")
    old["postgres"].update(pg_updates)

    old["etl"]["batch_size"] = int(form.get("etl_batch") or 500)
    old["etl"]["sync_window_days"] = int(form.get("etl_sync_days") or 30)
    old["etl"]["full_scan"] = bool(form.get("etl_full_scan"))
    return old


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # pg_restore uploads (multipart)
    try:
        app.json.ensure_ascii = False  # type: ignore[attr-defined]
    except Exception:
        pass

    def config_path() -> Path:
        w = os.environ.get("CONFIG_WRITE_PATH")
        if w:
            return Path(w).expanduser().resolve()
        return default_config_path()

    @app.get("/")
    def index():  # type: ignore[no-untyped-def]
        p = config_path()
        if not p.is_file():
            return (
                f"<p>Нет файла конфигурации: <code>{logical_config_path()}</code>. Скопируйте "
                f"<code>config/egisz_monitor.example.yaml</code> → <code>config/egisz_monitor.yaml</code>.</p>",
                404,
            )
        cfg = load_corp_config(p)
        mb_url = ""
        if isinstance(cfg.metabase, dict):
            u = cfg.metabase.get("site_url")
            if isinstance(u, str) and u.strip():
                mb_url = u.strip()
        if not mb_url:
            env_u = (os.environ.get("EGISZ_METABASE_SITE_URL") or "").strip()
            if env_u:
                mb_url = env_u
        return render_template_string(
            PAGE,
            path=str(logical_config_path()),
            fb=cfg.firebird,
            pg=cfg.postgres,
            etl=cfg.etl,
            metabase_site_url=mb_url,
        )

    @app.post("/save")
    def save():  # type: ignore[no-untyped-def]
        p = config_path()
        if not p.is_file():
            return jsonify(
                {
                    "ok": False,
                    "message": f"Нет файла конфигурации ({logical_config_path()}).",
                }
            )
        try:
            merged = _merged_yaml_dict_from_form(p, request.form)
        except (ValueError, TypeError) as e:
            return jsonify({"ok": False, "message": f"Проверьте поля формы (числа, обязательные значения): {e}"})
        try:
            save_corp_config(merged, p)
        except OSError as e:
            return jsonify(
                {
                    "ok": False,
                    "message": (
                        "Не удалось записать файл конфигурации (нет прав или том только для чтения). "
                        f"Детали: {e}"
                    ),
                }
            )
        os.environ["EGISZ_MONITOR_CONFIG"] = str(logical_config_path())
        try:
            parse_corp_config_dict(merged)
        except Exception as e:
            return jsonify({"ok": False, "message": f"Файл записан, но конфиг не читается: {e}"})
        return jsonify({"ok": True, "message": "Сохранено."})

    @app.post("/test-fb")
    def test_fb():  # type: ignore[no-untyped-def]
        p = config_path()
        if not p.is_file():
            return jsonify({"ok": False, "message": "Нет файла конфигурации на сервере."})
        try:
            merged = _merged_yaml_dict_from_form(p, request.form)
            cfg = parse_corp_config_dict(merged, use_yaml_postgres_only=True)
            fetch_all(cfg.firebird, "SELECT 1 AS OK FROM RDB$DATABASE")
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "message": f"Firebird: {e}"})
        return jsonify({"ok": True, "message": "Firebird: OK"})

    @app.post("/test-pg")
    def test_pg():  # type: ignore[no-untyped-def]
        p = config_path()
        if not p.is_file():
            return jsonify({"ok": False, "message": "Нет файла конфигурации на сервере."})
        try:
            merged = _merged_yaml_dict_from_form(p, request.form)
            cfg = parse_corp_config_dict(merged, use_yaml_postgres_only=True)
            test_pg_connection(cfg.postgres)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "message": f"PostgreSQL: {e}"})
        return jsonify({"ok": True, "message": "PostgreSQL: OK"})

    @app.post("/api/pg/backup")
    def api_pg_backup():  # type: ignore[no-untyped-def]
        """Download custom-format pg_dump (-Fc) for current form Postgres settings."""
        p = config_path()
        if not p.is_file():
            return jsonify({"ok": False, "message": "Нет файла конфигурации на сервере."}), 400
        try:
            merged = _merged_yaml_dict_from_form(p, request.form)
            cfg = parse_corp_config_dict(merged, use_yaml_postgres_only=True)
            blob = pg_dump_custom_bytes(cfg.postgres)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "message": f"pg_dump: {e}"}), 500
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        db = "".join(c if c.isalnum() or c in "._-" else "_" for c in (cfg.postgres.database or "db"))
        fn = f"egisz_pg_{db}_{ts}.dump"
        return Response(
            blob,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{fn}"'},
        )

    @app.post("/api/pg/restore")
    def api_pg_restore():  # type: ignore[no-untyped-def]
        """Restore from uploaded custom-format dump (data-only, fixed flags)."""
        p = config_path()
        if not p.is_file():
            return jsonify({"ok": False, "message": "Нет файла конфигурации на сервере."})
        f = request.files.get("dump")
        if f is None or not getattr(f, "filename", None):
            return jsonify({"ok": False, "message": "Выберите файл дампа (.dump)."})
        try:
            data = f.read()
            merged = _merged_yaml_dict_from_form(p, request.form)
            cfg = parse_corp_config_dict(merged, use_yaml_postgres_only=True)
            msg = restore_upload_to_temp_and_run(cfg.postgres, data)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "message": f"pg_restore: {e}"})
        return jsonify({"ok": True, "message": msg[:8000]})

    @app.get("/healthz")
    def healthz():  # type: ignore[no-untyped-def]
        """Лёгкий пробник для k8s readiness/liveness — без обращения к БД и без файлового I/O.

        Если pod процесс жив и Flask отвечает — пробник 200; недоступность Postgres/Firebird
        диагностируется отдельно через /api/healthcheck. Это устраняет каскад «pod NotReady →
        endpoints removed → 1.2s polling в браузере получает TypeError: Failed to fetch».
        """
        return jsonify({"ok": True})

    @app.get("/api/pg/sync-snapshot")
    def api_pg_sync_snapshot():  # type: ignore[no-untyped-def]
        """Только PostgreSQL: etl_state + витрина. Без Firebird — иначе UI блокируется на десятки секунд при опросе 1.5s."""
        p = config_path()
        if not p.is_file():
            return jsonify({"ok": False, "error": "Нет файла конфигурации на сервере."})
        try:
            cfg = load_corp_config(p)
            con = connect_pg(cfg.postgres)
            try:
                snap = fetch_pg_sync_snapshot(con, cfg.etl.pipeline_name)
            finally:
                con.close()
            return jsonify({"ok": True, **snap})
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)})

    @app.get("/api/healthcheck")
    def api_healthcheck():  # type: ignore[no-untyped-def]
        """Снимок healthcheck: сигналы, top-N клиник, прокси-БД (для Config UI и для алёртов)."""
        from datetime import datetime, timezone

        p = config_path()
        if not p.is_file():
            return jsonify({"ok": False, "error": "Нет файла конфигурации на сервере."})
        try:
            cfg = load_corp_config(p)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": f"Конфиг не читается: {e}"})

        result: dict[str, Any] = {
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "signals": [],
            "by_clinic_top": [],
            "proxy_db": {},
            "level_summary": {"red": 0, "yellow": 0, "green": 0},
            "errors": [],
        }
        try:
            con = connect_pg(cfg.postgres)
        except Exception as e:  # pragma: no cover - сеть/PG
            result["ok"] = False
            result["errors"].append(f"PostgreSQL недоступен: {e}")
            return jsonify(result)

        pg_cached_peaks: dict[str, Any] = {}
        try:
            snap = fetch_healthcheck_snapshot(con)
            try:
                pg_cached_peaks = fetch_etl_source_peaks_from_pg(con, cfg.etl.pipeline_name)
            except Exception:
                pg_cached_peaks = {}
        except Exception as e:  # pragma: no cover
            result["ok"] = False
            result["errors"].append(str(e))
            return jsonify(result)
        finally:
            con.close()

        result.update(snap)
        if snap.get("errors"):
            result["ok"] = False

        # Опрос Firebird — timeout; при неудаче подставляем source_max_* из etl_state (последний успешный ETL).
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout

        fb_peaks: dict[str, Any] = {"max_egmid": None, "max_licenses_modifydate": None, "error": None}
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(fetch_firebird_source_peaks, cfg.firebird)
            try:
                fb_peaks = fut.result(timeout=20.0)
            except _FutureTimeout:
                fb_peaks = {"max_egmid": None, "max_licenses_modifydate": None, "error": "timeout"}
            except Exception as e:  # pragma: no cover
                fb_peaks = {"max_egmid": None, "max_licenses_modifydate": None, "error": str(e)}
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        if fb_peaks.get("max_egmid") is None and pg_cached_peaks.get("source_max_egmid") is not None:
            fb_peaks["max_egmid"] = pg_cached_peaks["source_max_egmid"]
        if fb_peaks.get("max_licenses_modifydate") is None and pg_cached_peaks.get(
            "source_max_licenses_modifydate"
        ):
            fb_peaks["max_licenses_modifydate"] = pg_cached_peaks["source_max_licenses_modifydate"]

        peaks_resolved = fb_peaks.get("max_egmid") is not None or bool(
            fb_peaks.get("max_licenses_modifydate")
        )

        proxy_db = result.get("proxy_db") or {}
        if fb_peaks.get("max_egmid") is not None:
            proxy_db["fb_max_egmid"] = fb_peaks["max_egmid"]
            staging_max = proxy_db.get("staging_max_egmid")
            if isinstance(staging_max, int) and isinstance(fb_peaks["max_egmid"], int):
                proxy_db["egmid_lag"] = max(0, fb_peaks["max_egmid"] - staging_max)
        if fb_peaks.get("max_licenses_modifydate"):
            proxy_db["fb_max_licenses_modifydate"] = fb_peaks["max_licenses_modifydate"]
        if fb_peaks.get("error") and not peaks_resolved:
            result.setdefault("errors", []).append(f"Firebird peaks: {fb_peaks['error']}")
        result["proxy_db"] = proxy_db

        return jsonify(result)

    from egisz_monitor_corp.sync_routes import register_sync_routes

    register_sync_routes(app, config_path, _merged_yaml_dict_from_form)

    return app


def run_dev() -> None:
    import os

    app = create_app()
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "8765"))
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
