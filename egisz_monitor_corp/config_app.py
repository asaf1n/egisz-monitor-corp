"""Flask app: web page to view/edit Firebird + Postgres YAML config."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, jsonify, render_template_string, request

from egisz_monitor_corp.config_loader import (
    default_config_path,
    load_corp_config,
    logical_config_path,
    parse_corp_config_dict,
    save_corp_config,
)
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.fb_client import fetch_firebird_source_peaks
from egisz_monitor_corp.pg_warehouse import (
    connect_pg,
    fetch_healthcheck_snapshot,
    fetch_pg_sync_snapshot,
    test_pg_connection,
)

PAGE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <title>FB2PG Sync Configuration</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    /* ~50px tall inputs; shell uses most of viewport width with a capped max for ultra-wide */
    body { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    .cfg-in {
      min-height: 3.125rem;
      padding-top: 0.625rem;
      padding-bottom: 0.625rem;
      padding-left: 0.8125rem;
      padding-right: 0.8125rem;
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
    .snap-tab-btn {
      border-color: #1B2940;
      background: #0F1522;
      color: #6B7280;
    }
    .snap-tab-btn.is-active {
      border-color: #509EE3;
      background: #1B2940;
      color: #E5F6FF;
    }
    .snap-tab-panel.is-hidden { display: none; }
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
<body class="min-h-screen lg:h-screen bg-[#121826] text-white lg:overflow-hidden">
  <div class="w-full max-w-[min(88rem,calc(100vw-2rem))] mx-auto px-[clamp(1rem,4vw,2.5rem)] py-3 sm:py-5 lg:h-screen lg:flex lg:flex-col lg:py-3">
    <nav class="flex shrink-0 items-center justify-center gap-3 text-xs mb-3">
      <span class="text-[#509EE3]">FB2PG Sync</span>
      <span class="text-[#4B5563]">|</span>
      <a href="/" class="text-[#4B5563] transition hover:text-[#509EE3]">Обновить страницу</a>
    </nav>

    <section class="rounded-xl bg-[#0F1522] px-4 py-4 sm:px-6 lg:px-8 sm:py-5 lg:py-4 shadow-lg border border-[#1B2940] lg:flex lg:min-h-0 lg:flex-1 lg:flex-col">
      <div class="mb-4 lg:mb-3 border-b border-[#1B2940] pb-3 lg:pb-2 shrink-0">
        <h1 class="text-base sm:text-lg font-semibold text-white">FB2PG Sync Configuration</h1>
        <p class="mt-0.5 text-xs sm:text-sm text-[#9CA3AF]">
          Файл: <code class="text-[#509EE3]">{{ path }}</code><br/>
          Переопределение: переменная окружения <code class="text-[#509EE3]">EGISZ_CORP_CONFIG</code> или <code class="text-[#509EE3]">CONFIG_WRITE_PATH</code>.
        </p>
      </div>

      <form id="configForm" class="space-y-4 lg:space-y-0 lg:flex lg:min-h-0 lg:flex-1 lg:flex-col lg:gap-3" action="#" onsubmit="return false;">

        <div class="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:gap-x-8 lg:gap-y-0 lg:items-start xl:gap-x-10 lg:shrink-0">
        <!-- Firebird Section -->
        <div class="min-w-0 lg:pr-6 lg:border-r lg:border-[#1B2940]">
          <div class="mb-2">
            <h2 class="text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">Firebird Configuration</h2>
            <p class="mt-0.5 text-[11px] sm:text-xs text-[#9CA3AF] leading-snug">TCP к серверу Firebird (как в DBeaver). Если в Docker — используйте <code class="text-white">host.docker.internal</code></p>
          </div>

          <div class="grid gap-2.5 grid-cols-1 md:grid-cols-[minmax(0,1fr)_minmax(5.5rem,6rem)] md:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">host</span>
              <input name="fb_host" value="{{ fb.host }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full md:w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">port</span>
              <input name="fb_port" type="number" value="{{ fb.port }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid gap-2.5 grid-cols-1 md:grid-cols-[minmax(0,1fr)_minmax(0,10rem)] md:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">database</span>
              <input name="fb_database" value="{{ fb.database }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">charset</span>
              <input name="fb_charset" value="{{ fb.charset }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 flex flex-col sm:flex-row flex-wrap justify-center items-stretch sm:items-end gap-3 sm:gap-4 w-full max-w-xl mx-auto">
            <label class="block w-full sm:w-36 sm:flex-none shrink-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">user</span>
              <input name="fb_user" value="{{ fb.user }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full sm:flex-1 sm:min-w-[12rem] sm:max-w-md">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">password</span>
              <input name="fb_password" type="password" value="{{ fb.password }}" autocomplete="current-password" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
        </div>

        <!-- PostgreSQL Section -->
        <div class="min-w-0 pt-4 border-t border-[#1B2940] lg:pt-0 lg:pl-6 lg:border-0">
          <div class="mb-2">
            <h2 class="text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">PostgreSQL Configuration</h2>
            <p class="mt-0.5 text-[11px] sm:text-xs text-[#9CA3AF] leading-snug">Из пода витрины обычно <code class="text-white">postgres.egisz-monitor.svc.cluster.local:5432</code></p>
          </div>

          <div class="grid gap-2.5 grid-cols-1 md:grid-cols-[minmax(0,1fr)_minmax(5.5rem,6rem)] md:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">host</span>
              <input name="pg_host" value="{{ pg.host }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">port</span>
              <input name="pg_port" type="number" value="{{ pg.port }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid gap-2.5 grid-cols-1 md:grid-cols-[minmax(0,1fr)_minmax(0,10rem)] md:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">database</span>
              <input name="pg_database" value="{{ pg.database }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">schema</span>
              <input name="pg_schema" value="{{ pg.schema }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid gap-2.5 grid-cols-1 md:grid-cols-[minmax(0,11rem)_minmax(0,1fr)] md:items-end">
            <label class="block w-full md:max-w-none md:justify-self-stretch">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">user</span>
              <input name="pg_user" value="{{ pg.user }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">password</span>
              <input name="pg_password" type="password" value="{{ pg.password }}" autocomplete="current-password" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
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

        <div class="pt-3 lg:pt-2 grid grid-cols-1 sm:grid-cols-3 gap-2 sm:gap-3 border-t border-[#1B2940] lg:shrink-0">
          <button type="button" id="btnSaveYaml" class="inline-flex min-h-[2.875rem] min-w-0 w-full sm:min-w-[140px] items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white">
            Сохранить в YAML
          </button>
          <button type="button" id="btnTestFb" class="inline-flex min-h-[2.875rem] min-w-0 w-full sm:min-w-[140px] items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white">
            Проверить Firebird
          </button>
          <button type="button" id="btnTestPg" class="inline-flex min-h-[2.875rem] min-w-0 w-full sm:min-w-[140px] items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white">
            Проверить PostgreSQL
          </button>
        </div>

        <div class="pt-4 lg:pt-3 border-t border-[#1B2940] flex flex-col gap-4 lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(18rem,22rem)_minmax(18rem,1fr)] lg:gap-4 lg:flex-1 lg:min-h-0">
          <div class="flex min-w-0 flex-col gap-3 lg:min-h-0">
            <button type="button" id="btnSync" class="inline-flex w-full min-h-[2.875rem] items-center justify-center rounded-md border border-[#F59F36] bg-[#F59F36] px-3.5 py-2.5 font-mono text-sm text-[#121826] transition hover:bg-[#FFB95D]">
              Запустить синхронизацию
            </button>
            <div class="min-w-0">
              <div class="mb-2">
                <h2 class="text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">ETL Configuration</h2>
              </div>
              <p class="text-[11px] text-[#9CA3AF] leading-snug mb-3 max-w-2xl">Полный цикл Firebird → PostgreSQL в фоне. Не закрывайте вкладку до завершения.</p>
              <div class="grid gap-3 grid-cols-1 sm:grid-cols-2 sm:items-end max-w-lg">
                <label class="block w-full max-w-[8rem] sm:max-w-none">
                  <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">batch_size</span>
                  <input name="etl_batch" type="number" value="{{ etl.batch_size }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm tabular-nums text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
                </label>
                <label class="block w-full max-w-[10rem] sm:max-w-none">
                  <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">sync_window_days</span>
                  <input name="etl_sync_days" type="number" value="{{ etl.sync_window_days }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm tabular-nums text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
                </label>
              </div>
              <div class="mt-3 flex items-center gap-2.5 w-full min-h-[2.75rem]">
                <input type="checkbox" name="etl_full_scan" value="1" {{ 'checked' if etl.full_scan else '' }} class="h-4 w-4 shrink-0 rounded border-[#1B2940] bg-[#121826] text-[#509EE3] focus:ring-[#509EE3] focus:ring-offset-[#0F1522]"/>
                <span class="text-xs text-[#9CA3AF]">Полный скан</span>
              </div>
            </div>
          </div>

          <div class="w-full rounded-lg border border-[#2D3F5E] bg-[#121826] px-3 py-3 lg:py-2 flex shrink-0 flex-col gap-2 lg:gap-1.5 lg:h-full lg:max-h-none lg:min-h-0 lg:overflow-y-auto fixed-scroll">
            <div class="flex items-center gap-1.5 shrink-0">
              <button type="button" id="tabBtnSnapshot" class="snap-tab-btn flex-1 rounded-md border px-2 py-1 text-[10px] uppercase tracking-[0.14em] font-semibold transition" data-tab="snapshot">Snapshot</button>
              <button type="button" id="tabBtnHealth" class="snap-tab-btn flex-1 rounded-md border px-2 py-1 text-[10px] uppercase tracking-[0.14em] font-semibold transition" data-tab="health">Healthcheck</button>
            </div>

            <section id="tabSnapshot" class="snap-tab-panel flex flex-col gap-2 lg:gap-1.5">
              <p id="pgSnapshotHint" class="text-[11px] text-[#6B7280] leading-snug">Витрина и курсор в PostgreSQL; EGMID и MODIFYDATE лицензий — MAX() в Firebird по YAML (при сбое — см. текст).</p>
              <div class="space-y-2 text-sm leading-relaxed">
                <div class="flex items-baseline gap-2 min-w-0 py-0.5">
                  <span class="text-[#9CA3AF] shrink-0 font-medium text-[11px]">Дата:</span>
                  <code id="pgSnapSyncAt" class="min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-xs" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy shrink-0 inline-flex items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-1 text-[#509EE3] hover:bg-[#1B2940]" data-copy="pgSnapSyncAt" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex items-baseline gap-2 min-w-0 py-0.5">
                  <span class="text-[#9CA3AF] shrink-0 font-medium text-[11px]">LOGID:</span>
                  <code id="pgSnapLogId" class="min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-xs" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy shrink-0 inline-flex items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-1 text-[#509EE3] hover:bg-[#1B2940]" data-copy="pgSnapLogId" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex items-baseline gap-2 min-w-0 py-0.5">
                  <span class="text-[#9CA3AF] shrink-0 font-medium text-[11px]">EGMID:</span>
                  <code id="pgSnapEgmid" class="min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-xs" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy shrink-0 inline-flex items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-1 text-[#509EE3] hover:bg-[#1B2940]" data-copy="pgSnapEgmid" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex items-baseline gap-2 min-w-0 py-0.5">
                  <span class="text-[#9CA3AF] shrink-0 font-medium text-[11px]">LICENSES.MODIFYDATE:</span>
                  <code id="pgSnapLicMd" class="min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-xs" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy shrink-0 inline-flex items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-1 text-[#509EE3] hover:bg-[#1B2940]" data-copy="pgSnapLicMd" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
              </div>
            </section>

            <section id="tabHealth" class="snap-tab-panel hidden flex-col gap-2.5 lg:gap-2">
              <p id="hcHint" class="text-[11px] text-[#6B7280] leading-snug">Сигналы по витрине + топ-3 проблемные клиники + сводка прокси-БД (см. <code class="text-[#509EE3]">v_health_signals</code>, <code class="text-[#509EE3]">v_health_by_clinic</code>).</p>
              <div id="hcLevelSummary" class="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.14em]">
                <span class="rounded-full px-2 py-0.5 border border-rose-700/60 bg-rose-900/20 text-rose-200" data-level="red">red <span data-count="red">0</span></span>
                <span class="rounded-full px-2 py-0.5 border border-amber-600/60 bg-amber-900/20 text-amber-200" data-level="yellow">yellow <span data-count="yellow">0</span></span>
                <span class="rounded-full px-2 py-0.5 border border-emerald-700/60 bg-emerald-900/20 text-emerald-300" data-level="green">green <span data-count="green">0</span></span>
              </div>
              <div>
                <h3 class="text-[10px] uppercase tracking-[0.14em] text-[#509EE3] mb-1">Сигналы</h3>
                <ul id="hcSignals" class="space-y-1.5 text-[11px] leading-snug"></ul>
              </div>
              <div>
                <h3 class="text-[10px] uppercase tracking-[0.14em] text-[#509EE3] mb-1">Топ клиник по проблемам</h3>
                <ul id="hcClinics" class="space-y-1.5 text-[11px] leading-snug"></ul>
              </div>
              <div>
                <h3 class="text-[10px] uppercase tracking-[0.14em] text-[#509EE3] mb-1">Прокси-БД и курсор</h3>
                <ul id="hcProxyDb" class="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] leading-snug"></ul>
              </div>
            </section>
          </div>

          <div class="rounded-md bg-[#0B1120] px-3 py-2 text-sm text-[#93A1B6] border border-[#1B2940] min-h-0 min-w-0 lg:flex lg:flex-1 lg:flex-col">
            <div class="mb-1 flex items-center justify-between gap-2 min-w-0">
              <div class="text-[10px] uppercase tracking-[0.16em] text-[#509EE3]">system log</div>
              <button type="button" class="pg-snap-copy shrink-0 inline-flex items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-1 text-[#509EE3] hover:bg-[#1B2940]" data-copy="syncStatus" title="Копировать лог" aria-label="Копировать лог">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
              </button>
            </div>
            <pre id="syncStatus" class="whitespace-pre-wrap font-mono text-[11px] text-[#D1D5DB] min-h-[2.25rem] max-h-28 lg:max-h-none lg:flex-1 overflow-y-auto leading-relaxed"></pre>
          </div>
        </div>
      </form>
    </section>
  </div>

  <script>
  let lastSyncJson = { running: false, error: null, message: '', last_stats: null };
  let lastUiMessage = { ok: true, text: '' };
  const STRIP_BASE =
    'relative rounded-md border min-h-[2.75rem] lg:h-[2.75rem] overflow-hidden text-xs sm:text-sm font-mono transition-[background-color,border-color,color] duration-150';
  function syncProgressPercent(p) {
    if (!p || typeof p !== 'object') return null;
    const phase = p.phase || '';
    if (phase === 'outbound_fetch' || phase === 'outbound_parse' || phase === 'outbound_postgres' || phase === 'outbound_done') {
      const total = Number(p.outbound_total) || 0;
      const loaded = Number(p.outbound_loaded) || 0;
      if (phase === 'outbound_done' || (phase === 'outbound_postgres' && total === 0)) return 100;
      if (total > 0) return Math.min(100, Math.round((loaded / total) * 100));
      return 0;
    }
    if (phase === 'counting') return 14;
    const totalRows = Number(p.total_rows);
    const loadedRows = Number(p.loaded_rows) || 0;
    if (Number.isFinite(totalRows) && totalRows > 0) return Math.min(100, Math.round((loadedRows / totalRows) * 100));
    if (Number.isFinite(totalRows) && totalRows === 0) return phase === 'exchangelog_done' ? 100 : 0;
    return null;
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
      textEl.textContent = 'Синхронизация' + (phase ? ': ' + (PHASE_RU[phase] || phase) : ': подготовка');
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
    counting: 'Подсчёт строк журнала EXCHANGELOG в Firebird (для прогресса)…',
    exchangelog_ready: 'К журналу EXCHANGELOG: подготовка пагинации',
    fetch_page: 'Загрузка страницы EXCHANGELOG из Firebird…',
    parsing: 'Парсинг SOAP/MSGTEXT и обогащение журнала…',
    page_done: 'UPSERT фактов и измерений в PostgreSQL — страница сохранена',
    exchangelog_done: 'Журнал обработан, курсор LOGID обновлён',
    outbound_firebird: 'Исходящие EGISZ_MESSAGES: чтение из Firebird…',
    outbound_fetch: 'Исходящие сообщения (EGISZ_MESSAGES): выборка по окну sync_window_days…',
    outbound_parse: 'Разбор исходящих: дедуп DOCUMENTID, фильтр тестовых клиник…',
    outbound_postgres: 'Исходящие: запись stg_egisz_outbound_documents в PostgreSQL…',
    outbound_done: 'Исходящие: snapshot staging обновлён',
  };
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
        pct = 14;
      } else if (tr.kind === 'positive') {
        pct = Math.min(100, Math.round((lo / tr.n) * 100));
        lineA = 'Загружено из журнала: ' + lo + ' / ' + tr.n + ' строк';
      } else if (tr.kind === 'zero') {
        pct = phase === 'exchangelog_done' ? 100 : Math.min(100, lo > 0 ? 50 : 100);
        if (phase === 'exchangelog_ready' || phase === 'fetch_page' || phase === 'parsing') {
          lineA = 'Новых строк журнала нет (0 к обработке по курсору и окну sync_window_days).';
        } else {
          lineA = 'Строк журнала к загрузке: 0.';
        }
      } else {
        lineA = 'Объём журнала пока не передан сервером (редкий переход между фазами).';
        pct = 12;
      }

      lineB =
        'Фактов в витрине (после парсинга журнала): ' +
        journalFacts(p) +
        ' · staging ошибок: ' +
        (Number(p.staging_errors) || 0);
      if (p.page) lineB += ' · страница ' + p.page;
    }

    fill.style.width = pct + '%';
    pctEl.textContent = pct + '%';
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
  async function pollPgSnapshot() {
    const hint = document.getElementById('pgSnapshotHint');
    const syncEl = document.getElementById('pgSnapSyncAt');
    const logEl = document.getElementById('pgSnapLogId');
    const egEl = document.getElementById('pgSnapEgmid');
    const licEl = document.getElementById('pgSnapLicMd');
    if (!hint || !syncEl || !logEl || !egEl || !licEl) return;
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
      const r = await fetch('/api/pg/sync-snapshot');
      const j = await r.json();
      if (!j.ok) {
        hint.textContent = j.error || 'Не удалось прочитать PostgreSQL';
        hint.classList.add('text-orange-300');
        clearSnap();
        return;
      }
      let hintBase = 'Витрина и курсор в PostgreSQL; EGMID и MODIFYDATE лицензий — MAX() в Firebird по YAML.';
      if (j.firebird_peaks_error) {
        hintBase += ' Firebird: ' + j.firebird_peaks_error + ' (EGMID при необходимости из staging: ' + (j.egmid_staging_max != null ? String(j.egmid_staging_max) : '—') + ').';
      }
      hint.textContent = hintBase;
      hint.classList.remove('text-orange-300');
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
      const egStr = j.egmid != null ? String(j.egmid) : null;
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
    } catch (e) {
      hint.textContent = String(e);
      hint.classList.add('text-orange-300');
      clearSnap();
    }
  }
  let activeSnapTab = 'snapshot';
  function setSnapTab(tab) {
    activeSnapTab = (tab === 'health') ? 'health' : 'snapshot';
    document.querySelectorAll('.snap-tab-btn').forEach(function (btn) {
      const tabAttr = btn.getAttribute('data-tab');
      if (tabAttr === activeSnapTab) btn.classList.add('is-active');
      else btn.classList.remove('is-active');
    });
    const snapPanel = document.getElementById('tabSnapshot');
    const healthPanel = document.getElementById('tabHealth');
    if (snapPanel) snapPanel.classList.toggle('hidden', activeSnapTab !== 'snapshot');
    if (healthPanel) healthPanel.classList.toggle('hidden', activeSnapTab !== 'health');
  }
  document.querySelectorAll('.snap-tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const tabAttr = btn.getAttribute('data-tab') || 'snapshot';
      setSnapTab(tabAttr);
      if (tabAttr === 'health') pollHealthcheck();
    });
  });
  setSnapTab('snapshot');

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
      const r = await fetch('/api/healthcheck');
      const j = await r.json();
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
      hint.textContent = 'Ошибка опроса /api/healthcheck: ' + e;
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
  async function pollSync() {
    const el = document.getElementById('syncStatus');
    try {
      const r = await fetch('/api/sync/status');
      const j = await r.json();
      lastSyncJson = j;
      renderProgress(j);
      refreshConnStatusStrip();
      const parts = [];
      const msg = j.message != null ? String(j.message).trim() : '';
      if (msg) parts.push(msg);
      parts.push(j.running ? 'Статус: выполняется' : 'Статус: ожидание');
      if (j.error) {
        const errStr = String(j.error);
        const already = msg && msg.indexOf(errStr) >= 0;
        if (!already) parts.push('Ошибка: ' + errStr);
      }
      if (j.last_stats) parts.push(JSON.stringify(j.last_stats, null, 2));
      el.textContent = parts.filter(Boolean).join(String.fromCharCode(10));
    } catch (e) {
      el.textContent = 'Ошибка опроса: ' + e;
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
  document.getElementById('btnSync').onclick = async function() {
    const el = document.getElementById('syncStatus');
    el.textContent = 'Запрос...';
    const r = await fetch('/api/sync/start', { method: 'POST' });
    const j = await r.json();
    el.textContent = j.message || JSON.stringify(j);
    await pollSync();
  };
  setInterval(function () {
    pollSync();
    pollPgSnapshot();
  }, 1200);
  setInterval(function () {
    pollHealthcheck();
  }, 30000);
  pollSync();
  pollPgSnapshot();
  pollHealthcheck();
  refreshConnStatusStrip();
  window.addEventListener('pageshow', function () {
    pollSync();
    pollPgSnapshot();
    pollHealthcheck();
  });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      pollSync();
      pollPgSnapshot();
      pollHealthcheck();
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
                f"<code>config/egisz_corp.example.yaml</code> → <code>config/egisz_corp.yaml</code>.</p>",
                404,
            )
        cfg = load_corp_config(p)
        return render_template_string(
            PAGE,
            path=str(logical_config_path()),
            fb=cfg.firebird,
            pg=cfg.postgres,
            etl=cfg.etl,
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
        os.environ["EGISZ_CORP_CONFIG"] = str(logical_config_path())
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

    @app.get("/api/pg/sync-snapshot")
    def api_pg_sync_snapshot():  # type: ignore[no-untyped-def]
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
            staging_egmid = snap.get("egmid")
            fb_peaks = fetch_firebird_source_peaks(cfg.firebird)
            if fb_peaks.get("max_egmid") is not None:
                snap["egmid"] = fb_peaks["max_egmid"]
            snap["licenses_modifydate"] = fb_peaks.get("max_licenses_modifydate")
            snap["egmid_staging_max"] = staging_egmid
            if fb_peaks.get("error"):
                snap["firebird_peaks_error"] = fb_peaks["error"]
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

        try:
            snap = fetch_healthcheck_snapshot(con)
        except Exception as e:  # pragma: no cover
            result["ok"] = False
            result["errors"].append(str(e))
            return jsonify(result)
        finally:
            con.close()

        result.update(snap)
        if snap.get("errors"):
            result["ok"] = False

        # Опрос Firebird — с wall-clock timeout, чтобы недоступный источник не блокировал /api/healthcheck.
        # Используем явный shutdown(wait=False, cancel_futures=True), иначе `with` ждёт зависший TCP-connect.
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout

        fb_peaks: dict[str, Any] = {"max_egmid": None, "max_licenses_modifydate": None, "error": None}
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(fetch_firebird_source_peaks, cfg.firebird)
            try:
                fb_peaks = fut.result(timeout=5.0)
            except _FutureTimeout:
                fb_peaks = {"max_egmid": None, "max_licenses_modifydate": None, "error": "timeout"}
            except Exception as e:  # pragma: no cover
                fb_peaks = {"max_egmid": None, "max_licenses_modifydate": None, "error": str(e)}
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        proxy_db = result.get("proxy_db") or {}
        if fb_peaks.get("max_egmid") is not None:
            proxy_db["fb_max_egmid"] = fb_peaks["max_egmid"]
            staging_max = proxy_db.get("staging_max_egmid")
            if isinstance(staging_max, int) and isinstance(fb_peaks["max_egmid"], int):
                proxy_db["egmid_lag"] = max(0, fb_peaks["max_egmid"] - staging_max)
        if fb_peaks.get("max_licenses_modifydate"):
            proxy_db["fb_max_licenses_modifydate"] = fb_peaks["max_licenses_modifydate"]
        if fb_peaks.get("error"):
            result.setdefault("errors", []).append(f"Firebird peaks: {fb_peaks['error']}")
        result["proxy_db"] = proxy_db

        return jsonify(result)

    from egisz_monitor_corp.sync_routes import register_sync_routes

    register_sync_routes(app, config_path)

    return app


def run_dev() -> None:
    import os

    app = create_app()
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "8765"))
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
