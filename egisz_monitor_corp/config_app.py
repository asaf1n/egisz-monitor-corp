"""Flask app: web page to view/edit Firebird + Postgres YAML config."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, Response, jsonify, make_response, render_template_string, request

from egisz_monitor_corp.config_loader import (
    default_config_path,
    load_corp_config,
    logical_config_path,
    parse_corp_config_dict,
    save_corp_config,
)
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.fb_client import fetch_firebird_max_license_modifydate
from egisz_monitor_corp.k8s_cronjob import reconcile_egisz_monitor_sync_cronjob
from egisz_monitor_corp.metabase_export import build_export_zip_bytes
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
    #syncBannerProgressFill.sync-progress-indeterminate,
    #connStatusProgressFill.sync-progress-indeterminate {
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
    .hc-row { display: flex; align-items: flex-start; gap: 0.5rem; min-width: 0; }
    .hc-row .hc-bullet {
      flex-shrink: 0;
      width: 0.62rem;
      height: 0.62rem;
      border-radius: 9999px;
      margin-top: 0.28rem;
    }
    .hc-bullet-red { background: rgba(244, 63, 94, 0.85); box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.3); }
    .hc-bullet-yellow { background: rgba(245, 158, 11, 0.85); box-shadow: 0 0 0 1px rgba(245, 158, 11, 0.3); }
    .hc-bullet-green { background: rgba(16, 185, 129, 0.85); box-shadow: 0 0 0 1px rgba(16, 185, 129, 0.3); }
    /* Полоса «Стоп»: жёлтые диагонали как на предупреждающей разметке */
    #connStatusStrip.conn-strip-stop-hazard {
      border-color: rgba(250, 204, 21, 0.82);
      color: #fffbeb;
      background-color: rgba(6, 78, 59, 0.55);
      background-image: repeating-linear-gradient(
        -42deg,
        transparent 0 11px,
        rgba(250, 204, 21, 0.38) 11px 14px,
        transparent 14px 25px,
        rgba(250, 204, 21, 0.38) 25px 28px
      );
    }
    #connStatusStrip.conn-strip-stop-hazard #connStatusText {
      text-shadow: 0 0 2px rgba(0, 0, 0, 0.95), 0 1px 3px rgba(0, 0, 0, 0.85);
    }
  </style>
</head>
<body class="min-h-[100dvh] min-h-screen bg-[#121826] text-white lg:h-screen lg:overflow-hidden pb-[env(safe-area-inset-bottom,0px)]">
  <div class="mx-auto flex w-full min-h-[100dvh] min-w-0 max-w-[min(96rem,calc(100vw-1.5rem))] flex-col px-3 py-3 sm:px-[clamp(1rem,4vw,2.5rem)] sm:py-5 lg:min-h-0 lg:h-screen lg:flex-row lg:items-stretch lg:gap-4 lg:py-3">
    <div class="flex min-h-0 min-w-0 flex-1 flex-col lg:min-h-0 lg:overflow-y-auto lg:overflow-x-hidden lg:overscroll-y-contain">
    <nav class="mb-3 flex min-h-[2.75rem] shrink-0 flex-wrap items-center justify-center gap-3 text-sm text-[#D1D5DB] lg:text-xs">
      <span class="text-[#509EE3]">FB2PG Sync</span>
      <span class="text-[#4B5563]">|</span>
      <a href="/" class="text-[#4B5563] transition hover:text-[#509EE3]">Обновить страницу</a>
      <span class="hidden text-[#4B5563] lg:inline">|</span>
      <button type="button" id="btnRightAsideToggle" class="hidden rounded border border-[#2D3F5E] bg-[#1B2940] px-2.5 py-1.5 font-mono text-[11px] text-[#D1D5DB] transition hover:border-[#509EE3] hover:text-white lg:inline-flex">
        Скрыть панель
      </button>
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

        <div id="connStatusStrip" class="relative rounded-md border border-transparent bg-transparent min-h-[2.75rem] overflow-hidden text-xs sm:text-sm font-mono transition-[background-color,border-color,color] duration-150" role="status" aria-live="polite">
          <div id="connStatusProgressFill" class="absolute inset-y-0 left-0 top-0 z-0 transition-[width] duration-300 ease-out" style="width:0%"></div>
          <div class="relative z-[1] flex min-h-[2.75rem] w-full items-center justify-center px-8 py-2 pr-12 text-center lg:min-h-[2.75rem]">
            <span id="connStatusText" class="pointer-events-none block w-full min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-center text-inherit leading-tight" title=""></span>
            <span id="connStatusPct" class="hidden absolute right-2.5 top-1/2 z-[2] -translate-y-1/2 font-mono tabular-nums text-inherit" aria-hidden="true"></span>
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

        <div class="flex min-h-0 flex-1 flex-col gap-4 border-t border-[#1B2940] pt-4 lg:grid lg:min-h-0 lg:flex-1 lg:grid-cols-[minmax(0,1fr)_minmax(17rem,20rem)_minmax(16rem,1fr)] lg:grid-rows-[minmax(0,1fr)_auto] lg:items-stretch lg:gap-4 lg:pt-3">
          <div class="flex min-w-0 min-h-0 flex-col gap-1 lg:row-start-1 lg:col-start-1 lg:h-full lg:min-h-0 lg:self-stretch">
            <div class="grid min-h-12 w-full shrink-0 grid-cols-[minmax(0,4fr)_minmax(0,1fr)] gap-2 lg:min-h-[2.875rem]">
              <button type="button" id="btnSync" data-default-label="Запустить синхронизацию" class="inline-flex min-h-12 min-w-0 w-full items-center justify-center rounded-md border border-[#F59F36] bg-[#F59F36] px-2 py-2 font-mono text-[0.8125rem] leading-tight text-[#121826] transition hover:bg-[#FFB95D] disabled:cursor-not-allowed disabled:opacity-60 sm:text-sm lg:min-h-[2.875rem] lg:px-3">
                Запустить синхронизацию
              </button>
              <button type="button" id="btnSyncStop" disabled class="inline-flex min-h-12 w-full items-center justify-center rounded-md border border-rose-900/90 bg-rose-700 p-1 text-white shadow-sm transition hover:bg-rose-600 disabled:cursor-not-allowed disabled:opacity-45 lg:min-h-[2.875rem]" title="Остановить синхронизацию (кооперативно, после текущего шага)" aria-label="Остановить синхронизацию">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="70%" height="70%" preserveAspectRatio="xMidYMid meet" class="shrink-0" aria-hidden="true"><rect x="0" y="0" width="24" height="24" rx="5" ry="5" fill="#D1D5DB"/></svg>
              </button>
            </div>
            <div class="flex min-h-0 min-w-0 flex-1 flex-col lg:-mt-0.5">
              <div class="mb-1 shrink-0">
                <h2 class="text-sm font-medium uppercase tracking-[0.12em] text-[#9CA3AF] lg:text-[11px] lg:font-normal lg:tracking-[0.16em] lg:text-[#4B5563]">ETL Configuration</h2>
              </div>
              <p class="mb-2 w-full min-w-0 shrink-0 text-sm leading-snug text-[#9CA3AF] lg:text-[11px] lg:leading-snug">Полный цикл Firebird → PostgreSQL (журнал + исходящие).</p>
              <div class="grid w-full min-w-0 shrink-0 grid-cols-1 gap-2 sm:grid-cols-2 sm:items-end">
                <label class="block min-w-0 max-w-full">
                  <span class="block truncate font-mono text-[10px] uppercase tracking-[0.12em] text-[#9CA3AF] lg:text-[10px] lg:tracking-[0.14em] lg:text-[#4B5563]" title="batch_size">batch</span>
                  <input name="etl_batch" type="number" value="{{ etl.batch_size }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono tabular-nums text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
                </label>
                <label class="block min-w-0 max-w-full">
                  <span class="block truncate font-mono text-[10px] uppercase tracking-[0.12em] text-[#9CA3AF] lg:text-[10px] lg:tracking-[0.14em] lg:text-[#4B5563]" title="sync_window_days: EXCHANGELOG (LOGDATE), EGISZ_MESSAGES/исходящие (CREATEDATE); 0 = без фильтра по датам в Firebird для всех потоков + TRUNCATE stg_egisz_messages_journal и сброс messages_snapshot_high_egmid">sync days</span>
                  <input name="etl_sync_days" type="number" value="{{ etl.sync_window_days }}" title="0: синхронизация по всем записям (без окна по дате) для журнала и сообщений; полный пересъём снимка EGISZ_MESSAGES в staging" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono tabular-nums text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
                </label>
              </div>
              <div class="mt-3 flex min-h-[2.75rem] w-full shrink-0 flex-wrap items-center gap-3">
              </div>
            </div>
          </div>

          <div class="flex min-h-0 w-full min-w-0 shrink-0 flex-col gap-1.5 rounded-lg border border-[#2D3F5E] bg-[#121826] px-3 py-2 lg:row-start-1 lg:col-start-2 lg:mt-2 lg:h-full lg:min-h-0 lg:self-stretch lg:gap-1 lg:overflow-hidden lg:py-1.5 fixed-scroll">
            <div class="mb-0 shrink-0">
              <h2 class="text-sm font-semibold tracking-tight text-[#D1D5DB] lg:text-xs lg:leading-tight">Последние значения синхронизации</h2>
            </div>

            <section id="tabSnapshot" class="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto lg:gap-1 fixed-scroll">
              <div class="space-y-1 text-sm leading-snug lg:text-xs lg:leading-snug">
                <div class="flex min-w-0 items-baseline gap-2 py-0">
                  <span class="shrink-0 text-xs font-medium text-[#9CA3AF] sm:text-sm lg:text-xs">LOGID:</span>
                  <code id="pgSnapLogId" class="snap-val min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-sm sm:text-[15px]" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="pgSnapLogId" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex min-w-0 items-baseline gap-2 py-0">
                  <span class="shrink-0 max-w-[38%] truncate text-xs font-medium text-[#9CA3AF] sm:text-sm lg:text-xs" title="etl_state.last_egmid — ватермарк журнала, не paging снимка EGISZ_MESSAGES">last_egmid:</span>
                  <code id="pgSnapEgmid" class="snap-val min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-sm sm:text-[15px]" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="pgSnapEgmid" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex min-w-0 items-baseline gap-2 py-0">
                  <span class="max-w-[38%] shrink-0 truncate text-xs font-medium text-[#9CA3AF] sm:text-sm lg:max-w-[42%] lg:text-[10px]" title="etl_state.messages_snapshot_high_egmid — инкрементальная выгрузка снимка EGISZ_MESSAGES">MSG snap:</span>
                  <code id="pgSnapMsgScan" class="snap-val min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-sm sm:text-[15px]" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="pgSnapMsgScan" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
                <div class="flex min-w-0 items-baseline gap-2 py-0">
                  <span class="max-w-[38%] shrink-0 truncate text-xs font-medium text-[#9CA3AF] sm:text-sm lg:max-w-[42%] lg:text-[10px]" title="LICENSES.MODIFYDATE">LIC.MD:</span>
                  <code id="pgSnapLicMd" class="snap-val min-w-0 flex-1 truncate text-[#E5E7EB] font-mono text-sm sm:text-[15px]" data-raw="" title="">—</code>
                  <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="pgSnapLicMd" title="Копировать" aria-label="Копировать">
                    <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
                  </button>
                </div>
              </div>
            </section>
          </div>

          <div class="min-w-0 shrink-0 rounded-lg border border-[#1B2940] bg-[#121826]/80 px-3 py-2 lg:col-span-2 lg:col-start-1 lg:row-start-2 lg:min-h-0 lg:py-1.5">
            <div class="mb-1 text-[11px] font-medium uppercase tracking-[0.14em] text-[#509EE3]">Автосинхронизация (YAML)</div>
            <label class="flex min-w-0 cursor-pointer items-center gap-2 text-sm text-[#D1D5DB]">
              <input type="checkbox" name="auto_sync_enabled" value="1" class="h-4 w-4 shrink-0 rounded border-[#2D3F5E] bg-[#0F1522] text-[#509EE3] focus:ring-[#509EE3]" {% if auto_sync.enabled %}checked{% endif %}/>
              <span class="min-w-0 truncate font-mono text-xs uppercase tracking-[0.12em] text-[#9CA3AF]">auto_sync.enabled</span>
            </label>
            <input type="hidden" name="auto_sync_schedule_cron" value="{{ auto_sync.schedule_cron|default('*/15 * * * *', true) }}"/>
            <input type="hidden" name="auto_sync_timezone" value="{{ auto_sync.timezone|default('Etc/UTC', true) }}"/>
            <p class="mt-1 min-w-0 truncate text-[10px] leading-snug text-[#6B7280]" title="Полная конфигурация расписания и suspend CronJob в репозитории"><code class="font-mono text-[#9CA3AF]">k8s/etl-cron.yaml</code></p>
          </div>

          <div class="flex min-h-0 min-w-0 flex-1 flex-col rounded-md border border-[#1B2940] bg-[#0B1120] px-3 py-2 text-sm text-[#93A1B6] lg:row-start-1 lg:row-span-2 lg:col-start-3 lg:flex lg:h-full lg:min-h-0 lg:flex-1 lg:flex-col">
            <div class="mb-1 flex min-w-0 items-center justify-between gap-2">
              <div class="text-xs font-medium uppercase tracking-[0.12em] text-[#509EE3] lg:text-[10px] lg:font-normal lg:tracking-[0.16em]">system log</div>
              <button type="button" class="pg-snap-copy inline-flex min-h-[2.5rem] min-w-[2.5rem] shrink-0 items-center justify-center rounded border border-[#2D3F5E] bg-[#0F1522] p-2 text-[#509EE3] hover:bg-[#1B2940] sm:min-h-0 sm:min-w-0 sm:p-1" data-copy="syncStatus" title="Копировать лог" aria-label="Копировать лог">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="12" height="14" rx="2" ry="2"/></svg>
              </button>
            </div>
            <pre id="syncStatus" data-raw="" class="max-h-48 min-h-[6rem] flex-1 overflow-y-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-[#D1D5DB] sm:max-h-56 lg:max-h-none lg:min-h-[8rem] lg:text-[11px]"></pre>
          </div>
        </div>
      </form>
    </section>
    </div>

    <aside id="rightAsideShell" class="mt-5 flex w-full min-h-0 flex-1 flex-col gap-3 border-t border-[#1B2940] pt-5 lg:mt-0 lg:h-full lg:w-[min(30rem,38vw)] lg:max-w-[min(34rem,44vw)] lg:flex-none lg:shrink-0 lg:self-stretch lg:border-l lg:border-t-0 lg:pl-5 lg:pt-0">
      {% if metabase_site_url %}
      <a href="{{ metabase_site_url }}" target="_blank" rel="noopener noreferrer" class="inline-flex min-h-12 w-full shrink-0 items-center justify-center rounded-md border border-[#509EE3]/90 bg-[#1B2940] px-3 py-3 text-center text-sm font-semibold uppercase tracking-[0.12em] text-[#E5F6FF] transition hover:border-[#60B5FF] hover:bg-[#223555] lg:min-h-0 lg:py-2.5 lg:text-[11px] lg:tracking-[0.14em]">
        Metabase →
      </a>
      {% endif %}
      <div class="flex min-h-[14rem] flex-1 flex-col overflow-hidden rounded-lg border border-[#2D3F5E] bg-[#121826] px-4 py-4 lg:min-h-0">
        <h2 class="mb-3 shrink-0 text-base font-semibold uppercase tracking-[0.1em] text-[#D1D5DB]">Healthcheck</h2>
        <div class="fixed-scroll flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-1">
              <p id="hcHint" class="text-sm leading-snug text-[#9CA3AF]" aria-live="polite">Загрузка…</p>
              <div id="hcLevelSummary" class="flex flex-wrap items-center gap-2 text-xs font-medium uppercase tracking-[0.08em]">
                <span class="rounded-full px-2.5 py-1 border border-rose-700/60 bg-rose-900/20 text-rose-200" data-level="red">red <span data-count="red">0</span></span>
                <span class="rounded-full px-2.5 py-1 border border-amber-600/60 bg-amber-900/20 text-amber-200" data-level="yellow">yellow <span data-count="yellow">0</span></span>
                <span class="rounded-full px-2.5 py-1 border border-emerald-700/60 bg-emerald-900/20 text-emerald-300" data-level="green">green <span data-count="green">0</span></span>
              </div>
              <div>
                <h3 class="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-[#509EE3]">Сигналы</h3>
                <ul id="hcSignals" class="space-y-3 text-sm leading-snug"></ul>
              </div>
              <div>
                <h3 class="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-[#509EE3]">Топ клиник по проблемам</h3>
                <ul id="hcClinics" class="space-y-3 text-sm leading-snug"></ul>
              </div>
              <div>
                <h3 class="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-[#509EE3]">Прокси-БД и курсор</h3>
                <ul id="hcProxyDb" class="grid grid-cols-1 gap-x-4 gap-y-2.5 text-sm leading-snug sm:grid-cols-2"></ul>
              </div>
        </div>
      </div>
      <div class="shrink-0 rounded-lg border border-[#2D3F5E] bg-[#121826] px-3 py-3 lg:py-2.5">
        <div class="mb-2 flex flex-col gap-2 border-b border-[#1B2940] pb-3">
          <div class="flex min-w-0 items-stretch gap-2">
            <button type="button" id="btnMbExport" class="inline-flex min-h-10 min-w-0 flex-1 max-w-[calc(100%-2.75rem)] items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-2 py-2 font-mono text-xs text-[#D1D5DB] transition hover:border-[#509EE3] hover:bg-[#223555] hover:text-white sm:text-sm" title="Скачать ZIP: при доступном API — из Metabase, иначе эталон из образа">
              Скачать ZIP дашбордов
            </button>
            <button type="button" id="btnMbExportDir" class="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-[#2D3F5E] bg-[#0F1522] text-[#509EE3] transition hover:border-[#509EE3] hover:bg-[#1B2940]" title="Папка для сохранения ZIP (Chrome/Edge)">
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            </button>
          </div>
          <p id="mbExportDirHint" class="hidden truncate text-[10px] text-[#6B7280]" title=""></p>
          <p class="truncate font-mono text-[10px] leading-snug text-[#6B7280]" title="Сначала выгрузка из Metabase (нужны Secret или api_key). Если API недоступен — в архиве эталон из каталога репозитория metabase_dashboards/">
            Источник: Metabase API или эталон <code class="text-[#9CA3AF]">metabase_dashboards/</code> в образе
          </p>
        </div>
        <input type="file" id="pgRestoreFile" accept=".dump,.backup,application/octet-stream" class="sr-only" tabindex="-1" aria-hidden="true"/>
        <div class="flex flex-col gap-2">
          <div class="flex min-w-0 items-stretch gap-2">
            <button type="button" id="btnPgBackup" class="inline-flex min-h-10 min-w-0 flex-1 max-w-[calc(100%-2.75rem)] items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-2 py-2 font-mono text-xs text-[#D1D5DB] transition hover:border-[#509EE3] hover:bg-[#223555] hover:text-white sm:text-sm">
              Backup DWH
            </button>
            <button type="button" id="btnPgBackupDir" class="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-[#2D3F5E] bg-[#0F1522] text-[#509EE3] transition hover:border-[#509EE3] hover:bg-[#1B2940]" title="Папка для сохранения дампа (Chrome/Edge)">
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            </button>
          </div>
          <p id="pgBackupDirHint" class="hidden truncate text-[10px] text-[#6B7280]" title=""></p>
          <div class="flex min-w-0 items-stretch gap-2">
            <button type="button" id="btnPgRestore" class="inline-flex min-h-10 min-w-0 flex-1 max-w-[calc(100%-2.75rem)] items-center justify-center rounded-md border border-rose-700/70 bg-rose-950/40 px-2 py-2 font-mono text-xs text-rose-100 transition hover:bg-rose-900/50 sm:text-sm">
              Restore DWH
            </button>
            <button type="button" id="btnPgRestorePick" class="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-rose-800/60 bg-[#0F1522] text-rose-200 transition hover:border-rose-600 hover:bg-rose-950/50" title="Выбрать файл дампа">
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            </button>
          </div>
          <p id="pgRestoreFileHint" class="hidden truncate text-[10px] text-[#6B7280]" title=""></p>
        </div>
      </div>
    </aside>
  </div>

  <script>
  let lastSyncJson = { running: false, error: null, message: '', last_stats: null, sync_attempted: false };
  let wasSyncRunning = false;
  let lastUiMessage = { ok: true, strip: '', logBody: '' };
  let pgBackupDirHandle = null;
  let mbExportDirHandle = null;
  const STRIP_BASE =
    'relative rounded-md border min-h-[2.75rem] overflow-hidden text-xs sm:text-sm font-mono transition-[background-color,border-color,color] duration-150';
  function etlPhaseLabelRu(phase) {
    const m = {
      pipeline_bootstrap: 'Подготовка ETL (схема PostgreSQL, lock)',
      references_jpersons_export: 'JPERSONS: выгрузка из Firebird',
      references_licenses_export: 'EGISZ_LICENSES: выгрузка из Firebird',
      references_pg_staging: 'Справочники: запись staging в PostgreSQL',
      references_merge_dim: 'Справочники: merge dim_clinics из staging',
      journal_messages_export: 'EGISZ_MESSAGES: выгрузка из Firebird в PostgreSQL (staging журнала)',
      counting: 'Перед журналом: last_log_id и etl_state.last_egmid (COUNT журнала в Firebird не выполняется)',
      exchangelog_ready: 'Журнал EXCHANGELOG: подготовка к инкременту по LOGID',
      exchangelog_export: 'Чтение пакета EXCHANGELOG из Firebird',
      exchangelog_parse: 'Парсинг пакета журнала',
      parsing: 'Разбор строк журнала (пакет)',
      page_done: 'Пакет журнала обработан',
      exchangelog_done: 'Журнал — завершено',
      outbound_firebird: 'Исходящие документы: запрос к Firebird',
      outbound_fetch: 'Исходящие документы: выборка',
      outbound_parse: 'Исходящие документы: разбор',
      outbound_postgres: 'Исходящие документы: запись в PostgreSQL',
      outbound_done: 'Исходящие документы: готово',
      sync_failed: 'Синхронизация: ошибка или прерывание (снимок курсоров etl_state)',
      stopped_by_user: 'Синхронизация остановлена по запросу (снимок курсоров etl_state)',
    };
    return m[phase] || (phase ? 'Фаза: ' + phase : '');
  }
  function fmtIntRu(n) {
    if (n == null || !Number.isFinite(Number(n))) return null;
    return Math.trunc(Number(n)).toLocaleString('ru-RU');
  }
  /** Краткие строки system log: текущий элемент и числа (без дублирования курсоров и «полный объём не считается»). */
  function etlProgressLogConcise(p) {
    if (!p || typeof p !== 'object') return { title: '', detail: '' };
    const ph = String(p.phase || '');
    const lo = Number(p.loaded_rows);
    const tot = Number(p.total_rows);
    const page = Number(p.page);
    const facts = Number(p.parsed_facts != null ? p.parsed_facts : p.journal_facts);
    const stag = Number(p.staging_errors);
    const logid = p.cursor_log_id;
    const egm = p.etl_last_egmid;
    const br = p.messages_batch_rows;
    const jb = Number(p.journal_batch_rows);
    const cache = p.messages_msgid_cache_size;
    const outL = Number(p.outbound_loaded);
    const outT = Number(p.outbound_total);
    const srcEg = p.source_max_egmid;
    const wnote = p.watermark_note != null ? String(p.watermark_note).trim() : '';
    const diag = p.diag_error != null ? String(p.diag_error).trim() : '';
    if (ph === 'sync_failed' || ph === 'stopped_by_user') {
      const parts = [];
      if (logid != null && logid !== '') parts.push('last_log_id ' + String(logid));
      if (egm != null && egm !== '') parts.push('last_egmid ' + String(egm));
      if (srcEg != null && srcEg !== '') parts.push('source_max_egmid ' + String(srcEg));
      if (wnote) parts.push(wnote);
      if (diag) parts.push(diag);
      const title =
        ph === 'stopped_by_user'
          ? 'Синхронизация остановлена (курсоры по последнему коммиту)'
          : 'Синхронизация: ошибка / прерывание';
      return { title: title, detail: parts.join(' · ') };
    }
    if (ph === 'references_jpersons_export' || ph === 'references_licenses_export') {
      const parts = [];
      if (Number.isFinite(lo) && lo >= 0) parts.push('строк ' + fmtIntRu(lo));
      const title =
        ph === 'references_jpersons_export'
          ? 'JPERSONS: выгрузка из Firebird'
          : 'EGISZ_LICENSES: выгрузка из Firebird';
      return { title: title, detail: parts.join(' · ') };
    }
    if (ph === 'journal_messages_export') {
      const parts = [];
      if (Number.isFinite(lo) && lo > 0) parts.push('загружено строк ' + fmtIntRu(lo));
      if (Number.isFinite(page) && page > 0) parts.push('страница ' + fmtIntRu(page));
      const fbWait = Number(p.firebird_elapsed_sec);
      if (Number.isFinite(fbWait) && fbWait > 0) parts.push(fmtIntRu(fbWait) + ' с');
      return {
        title: 'EGISZ_MESSAGES: выгрузка из Firebird в PostgreSQL',
        detail: parts.join(' · '),
      };
    }
    if (ph === 'parsing') {
      const parts = [];
      if (Number.isFinite(page) && page > 0) parts.push('пакет ' + fmtIntRu(page));
      if (Number.isFinite(jb) && jb > 0) parts.push('строк в пакете ' + fmtIntRu(jb));
      if (Number.isFinite(lo) && lo > 0) parts.push('строк журнала обработано ' + fmtIntRu(lo));
      if (Number.isFinite(facts) && facts >= 0) parts.push('фактов ' + fmtIntRu(facts));
      if (logid != null && logid !== '') parts.push('LOGID ' + String(logid));
      return { title: 'EXCHANGELOG: разбор строк пакета (прогресс)', detail: parts.join(' · ') };
    }
    if (ph === 'exchangelog_export' || ph === 'exchangelog_parse' || ph === 'page_done') {
      const segs = [];
      if (Number.isFinite(cache) && cache > 0) segs.push('MSGID в кэше пакета ' + fmtIntRu(cache));
      if (Number.isFinite(page) && page > 0) segs.push('пакет ' + fmtIntRu(page));
      if (Number.isFinite(jb) && jb > 0) segs.push('строк в пакете ' + fmtIntRu(jb));
      if (Number.isFinite(lo) && lo > 0) segs.push('всего обработано журнала ' + fmtIntRu(lo));
      if (logid != null && logid !== '') segs.push('LOGID курсор ' + String(logid));
      if (Number.isFinite(facts) && facts >= 0) segs.push('фактов ' + fmtIntRu(facts));
      if (Number.isFinite(stag) && stag > 0) segs.push('ошибок staging ' + fmtIntRu(stag));
      const verb =
        ph === 'exchangelog_export' ? 'чтение пакета' : ph === 'exchangelog_parse' ? 'разбор пакета' : 'пакет обработан';
      return { title: 'EXCHANGELOG: ' + verb, detail: segs.join(', ') };
    }
    if (ph === 'exchangelog_ready') {
      const parts = [];
      parts.push('MSGID в пакете журнала сопоставляются со staging EGISZ_MESSAGES в PostgreSQL');
      if (egm != null && egm !== '') parts.push('last_egmid ' + String(egm));
      if (logid != null && logid !== '') parts.push('last_log_id ' + String(logid));
      if (Number.isFinite(tot) && tot > 0) parts.push('оценка строк журнала ' + fmtIntRu(tot));
      return { title: 'Журнал EXCHANGELOG: подготовка к инкременту', detail: parts.join(' · ') };
    }
    if (ph === 'exchangelog_done') {
      const parts = [];
      if (Number.isFinite(lo) && lo > 0) parts.push('строк журнала ' + fmtIntRu(lo));
      if (Number.isFinite(facts) && facts >= 0) parts.push('фактов ' + fmtIntRu(facts));
      return { title: 'EXCHANGELOG: завершено', detail: parts.join(' · ') };
    }
    if (ph.indexOf('outbound_') === 0) {
      const parts = [];
      if (Number.isFinite(outT) && outT > 0 && Number.isFinite(outL)) parts.push('документов ' + fmtIntRu(outL) + ' / ' + fmtIntRu(outT));
      if (Number.isFinite(facts) && facts >= 0 && ph !== 'outbound_fetch') parts.push('staging ' + fmtIntRu(facts));
      return { title: 'Исходящие документы', detail: parts.join(' · ') };
    }
    if (ph === 'counting') {
      const parts = [];
      parts.push('без COUNT по журналу в Firebird');
      if (egm != null && egm !== '') parts.push('last_egmid ' + String(egm));
      if (logid != null && logid !== '') parts.push('last_log_id ' + String(logid));
      return { title: 'Перед инкрементом журнала', detail: parts.join(' · ') };
    }
    const short = etlPhaseLabelRu(ph);
    return { title: short || ph, detail: '' };
  }
  /**
   * Полоска: только при известном знаменателе (доля журнала или исходящих).
   * Иначе — неопределённая анимация и подпись «…», без выдуманных процентов.
   */
  function etlBarVisual(p) {
    if (!p || typeof p !== 'object') {
      return { indeterminate: true, pct: null, label: '…' };
    }
    const ph = String(p.phase || '');
    if (ph === 'sync_failed' || ph === 'stopped_by_user') {
      return { indeterminate: true, pct: null, label: '…' };
    }
    if (ph === 'pipeline_bootstrap' || ph === 'counting') {
      return { indeterminate: true, pct: null, label: '…' };
    }
    if (ph === 'exchangelog_ready') {
      return { indeterminate: true, pct: null, label: '…' };
    }
    if (ph === 'exchangelog_done') {
      return { indeterminate: false, pct: 100, label: '100%' };
    }
    const tot = Number(p.total_rows);
    const lo = Number(p.loaded_rows) || 0;
    const journalPh =
      ph === 'exchangelog_export' ||
      ph === 'exchangelog_parse' ||
      ph === 'parsing' ||
      ph === 'page_done';
    if (journalPh && Number.isFinite(tot) && tot > 0) {
      const pct = Math.min(100, Math.round((lo / tot) * 100));
      return { indeterminate: false, pct, label: pct + '%' };
    }
    if (journalPh) {
      return { indeterminate: true, pct: null, label: '…' };
    }
    if (ph === 'outbound_firebird') {
      return { indeterminate: true, pct: null, label: '…' };
    }
    const ot = Number(p.outbound_total) || 0;
    const ol = Number(p.outbound_loaded) || 0;
    if (ph.indexOf('outbound_') === 0 && ph !== 'outbound_firebird') {
      if (ph === 'outbound_done') {
        return { indeterminate: false, pct: 100, label: '100%' };
      }
      if (ot > 0) {
        const pct = Math.min(100, Math.round((ol / ot) * 100));
        return { indeterminate: false, pct, label: pct + '%' };
      }
      return { indeterminate: true, pct: null, label: '…' };
    }
    return { indeterminate: true, pct: null, label: '…' };
  }
  /** Один визуальный ряд для system log. */
  function etlStatusOneLine(s) {
    return String(s)
      .replace(/\\r\\n|\\r|\\n/g, ' ')
      .replace(/\\s+/g, ' ')
      .trim();
  }
  /** Одна короткая фраза в строке состояния (без второго предложения и длинного текста). */
  function statusLinePhrase(raw, maxLen) {
    if (raw == null) return '';
    const t = etlStatusOneLine(String(raw));
    if (!t) return '';
    const lim = maxLen != null ? maxLen : 42;
    let u = t.split(/[;]|\\s+—\\s+|[.]{3,}/)[0].trim();
    if (u.length > lim) u = u.slice(0, Math.max(0, lim - 1)) + '…';
    return u;
  }
  /** Порядок: идёт синк → ошибка ETL → сообщение с кнопок (проверка/сейв) → завершённый синк → готов. */
  function connStatusStripState(j, ui) {
    const m = (ui && ui.strip) ? String(ui.strip).trim() : '';
    const logHint = 'См. system log';
    if (j && j.running) {
      return { key: 'sync', title: 'Синхронизация', hint: logHint, classBar: 'border-[#509EE3]/85 bg-[#0C4A6E]/60 text-[#E5F6FF]' };
    }
    if (j && j.error) {
      return { key: 'sync_err', title: 'Ошибка синхронизации', hint: logHint, classBar: 'border-orange-700/80 bg-orange-900/40 text-orange-100' };
    }
    if (m) {
      const mNorm = m.trim();
      if (mNorm === 'Стоп') {
        return {
          key: 'stop_strip',
          title: statusLinePhrase(mNorm, 72) || 'Стоп',
          hint: logHint,
          classBar: 'conn-strip-stop-hazard',
        };
      }
      return ui && ui.ok
        ? {
            key: 'db_ok',
            title: statusLinePhrase(m, 72) || 'Готово',
            hint: logHint,
            classBar: 'border-emerald-500/90 bg-emerald-700/40 text-emerald-50',
          }
        : { key: 'db_err', title: statusLinePhrase(m, 40) || 'Ошибка', hint: logHint, classBar: 'border-rose-800/80 bg-rose-900/30 text-rose-200' };
    }
    if (j && j.last_stats) {
      return { key: 'sync_done', title: 'Синхронизация завершена', hint: logHint, classBar: 'border-emerald-800/80 bg-emerald-900/25 text-emerald-300' };
    }
    return { key: 'idle', title: 'Готов к работе', hint: logHint, classBar: 'border-transparent bg-transparent text-[#9CA3AF]' };
  }
  function syncProgressMetaBlock(j) {
    const p = j && j.progress && typeof j.progress === 'object' ? j.progress : null;
    const msg = j && j.message != null ? String(j.message).trim() : '';
    const lines = [];
    if (p) {
      const c = etlProgressLogConcise(p);
      if (c.title) lines.push(c.title);
      if (c.detail) lines.push(c.detail);
    }
    if (msg && (/Предупреждение/i.test(msg) || !p)) lines.push(msg);
    return lines.filter(Boolean).join(String.fromCharCode(10));
  }
  function refreshConnStatusStrip() {
    const wrap = document.getElementById('connStatusStrip');
    const textEl = document.getElementById('connStatusText');
    const fill = document.getElementById('connStatusProgressFill');
    const pctEl = document.getElementById('connStatusPct');
    if (!wrap || !textEl || !fill || !pctEl) return;
    const j = lastSyncJson;
    const ui = lastUiMessage;
    const st = connStatusStripState(j, ui);
    wrap.className = STRIP_BASE;
    if (st.classBar) {
      st.classBar.split(/\\s+/).forEach(function (c) { if (c) wrap.classList.add(c); });
    }
    fill.classList.remove('sync-progress-indeterminate');
    fill.style.width = '0%';
    textEl.textContent = statusLinePhrase(st.title, st.key === 'db_ok' ? 72 : 40);
    textEl.setAttribute('title', st.hint && String(st.hint).trim() ? String(st.hint) : st.title);
    if (j && j.running) {
      const p = j.progress;
      const bar = etlBarVisual(p && typeof p === 'object' ? p : null);
      pctEl.className = 'absolute right-2.5 top-1/2 z-[2] -translate-y-1/2 font-mono tabular-nums text-inherit';
      pctEl.textContent = bar.label;
      if (bar.indeterminate) {
        fill.classList.add('sync-progress-indeterminate');
        fill.style.width = '';
      } else {
        fill.style.width = (bar.pct != null ? bar.pct : 0) + '%';
      }
    } else if (j && j.error) {
      pctEl.className = 'absolute right-2.5 top-1/2 z-[2] -translate-y-1/2 font-mono tabular-nums text-inherit';
      pctEl.textContent = '!';
    } else {
      pctEl.className = 'hidden';
      pctEl.textContent = '';
    }
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
  function syncMetricsLines() {
    const logEl = document.getElementById('pgSnapLogId');
    const egEl = document.getElementById('pgSnapEgmid');
    const msgScanEl = document.getElementById('pgSnapMsgScan');
    const licEl = document.getElementById('pgSnapLicMd');
    function rawFrom(el) {
      if (!el) return '—';
      const a = el.getAttribute('data-raw');
      if (a != null && String(a).trim() !== '') return String(a).trim();
      const t = el.textContent ? el.textContent.trim() : '';
      return t || '—';
    }
    let logV = rawFrom(logEl);
    let egV = rawFrom(egEl);
    let msgScanV = rawFrom(msgScanEl);
    let licV = rawFrom(licEl);
    function fmtNum(s) {
      if (s === '—') return '—';
      const n = Number(String(s).replace(/\\s/g, '').replace(/\\u00a0/g, ''));
      return Number.isFinite(n) ? n.toLocaleString('ru-RU') : String(s);
    }
    return [
      'LOGID: ' + fmtNum(logV),
      'last_egmid: ' + fmtNum(egV),
      'messages_snapshot_high_egmid: ' + fmtNum(msgScanV),
      'LICENSES.MODIFYDATE: ' + licV,
    ];
  }
  function formatSyncStatusBlock(j) {
    const lines = [];
    const msg = j.message != null ? String(j.message).trim() : '';
    if (j.running) {
      lines.push(syncProgressMetaBlock(j));
      if (msg && /Предупреждение/i.test(msg)) lines.push(msg);
    } else {
      if (j.error) {
        if (msg) lines.push(msg);
        const p = j.progress && typeof j.progress === 'object' ? j.progress : null;
        if (p && String(p.phase || '') === 'sync_failed') {
          const c = etlProgressLogConcise(p);
          if (c.title) lines.push(c.title);
          if (c.detail) lines.push(c.detail);
        }
        lines.push('Синхронизация завершилась с ошибкой.');
        const errStr = String(j.error);
        if (!msg || msg.indexOf(errStr) < 0) lines.push('Ошибка: ' + errStr);
      } else if (j.last_stats) {
        if (msg) lines.push(msg);
        else if (!j.last_stats.stopped_by_user) {
          lines.push('Синхронизация завершена успешно: полный проход конвейера Firebird → PostgreSQL.');
        }
        if (j.last_stats.stopped_by_user) {
          lines.push(
            'Итог остановки зафиксирован: последний коммит в PostgreSQL сохранён; ниже — снимок etl_state и прогресс на момент остановки.'
          );
        }
        lines.push(JSON.stringify(j.last_stats, null, 2));
      } else if (j.sync_attempted) {
        if (msg) lines.push(msg);
        lines.push(
          'Итог последнего запуска не зафиксирован (нет счётчиков): прерывание, конфликт lock или рестарт воркера.'
        );
      } else {
        lines.push('Статус: ожидание');
      }
    }
    return lines.filter(Boolean).join(String.fromCharCode(10));
  }
  function buildSystemLogText(j) {
    const core = formatSyncStatusBlock(j);
    if (lastUiMessage && lastUiMessage.logBody && String(lastUiMessage.logBody).trim() && !(j && j.running)) {
      return core + (core ? String.fromCharCode(10) + String.fromCharCode(10) : '') + '—' + String.fromCharCode(10) + 'Действия (кнопки):' + String.fromCharCode(10) + String(lastUiMessage.logBody);
    }
    return core;
  }
  function applySyncStatusFromPoll(j, el) {
    if (!el || syncStartInFlight) return;
    const display = buildSystemLogText(j);
    el.textContent = display;
    el.setAttribute('data-raw', display);
  }
  async function loadPgSyncSnapshotOnce() {
    const logEl = document.getElementById('pgSnapLogId');
    const egEl = document.getElementById('pgSnapEgmid');
    const msgScanEl = document.getElementById('pgSnapMsgScan');
    const licEl = document.getElementById('pgSnapLicMd');
    if (!logEl || !egEl || !licEl) return;
    function clearSnap() {
      logEl.textContent = '—';
      logEl.setAttribute('data-raw', '');
      logEl.title = '';
      egEl.textContent = '—';
      egEl.setAttribute('data-raw', '');
      egEl.title = '';
      if (msgScanEl) {
        msgScanEl.textContent = '—';
        msgScanEl.setAttribute('data-raw', '');
        msgScanEl.title = '';
      }
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
        return;
      }
      const logStr = j.log_id != null ? String(j.log_id) : null;
      if (logStr != null && logStr !== '') {
        const ln = Number(logStr);
        logEl.textContent = Number.isFinite(ln) ? ln.toLocaleString('ru-RU') : logStr;
        logEl.setAttribute('data-raw', logStr);
        logEl.title = logStr;
      } else {
        logEl.textContent = '—';
        logEl.setAttribute('data-raw', '');
        logEl.title = '';
      }
      const egStr = j.egmid != null ? String(j.egmid) : null;
      if (egStr != null && egStr !== '') {
        const en = Number(egStr);
        egEl.textContent = Number.isFinite(en) ? en.toLocaleString('ru-RU') : egStr;
        egEl.setAttribute('data-raw', egStr);
        egEl.title = egStr;
      } else {
        egEl.textContent = '—';
        egEl.setAttribute('data-raw', '');
        egEl.title = '';
      }
      if (msgScanEl) {
        const msStr = j.messages_snapshot_high_egmid != null ? String(j.messages_snapshot_high_egmid) : null;
        if (msStr != null && msStr !== '') {
          const mn = Number(msStr);
          msgScanEl.textContent = Number.isFinite(mn) ? mn.toLocaleString('ru-RU') : msStr;
          msgScanEl.setAttribute('data-raw', msStr);
          msgScanEl.title = msStr;
        } else {
          msgScanEl.textContent = '—';
          msgScanEl.setAttribute('data-raw', '');
          msgScanEl.title = '';
        }
      }
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
      POLL_FAIL.snap += 1;
      if (POLL_FAIL.snap <= 2) return;
      clearSnap();
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
        : ('Свежее: ' + (j.generated_at ? formatPgSyncAtRu(j.generated_at) : '—'));
      hint.classList.toggle('text-orange-300', !!(j.errors && j.errors.length));

      signalsList.innerHTML = '';
      const sigs = Array.isArray(j.signals) ? j.signals : [];
      if (!sigs.length) {
        signalsList.innerHTML = '<li class="text-sm text-[#6B7280]">Нет сигналов.</li>';
      } else {
        for (const s of sigs) {
          const li = document.createElement('li');
          const lvl2 = (s.level || 'green').toLowerCase();
          li.className = 'hc-row hc-row-' + lvl2;
          const bullet = document.createElement('span');
          bullet.className = 'hc-bullet hc-bullet-' + lvl2;
          li.appendChild(bullet);
          const body = document.createElement('div');
          body.className = 'min-w-0 flex-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5';
          const name = document.createElement('span');
          name.className = 'font-medium text-[#E5E7EB]';
          name.textContent = s.title || s.code;
          const val = document.createElement('span');
          val.className = 'shrink-0 font-mono tabular-nums text-xs text-[#9CA3AF]';
          val.textContent = fmtPctOrNum(s.value, s.value_unit);
          body.appendChild(name);
          body.appendChild(val);
          li.appendChild(body);
          signalsList.appendChild(li);
        }
      }

      clinicsList.innerHTML = '';
      const clinics = Array.isArray(j.by_clinic_top) ? j.by_clinic_top : [];
      if (!clinics.length) {
        clinicsList.innerHTML = '<li class="text-sm text-[#6B7280]">Нет агрегатов по клиникам.</li>';
      } else {
        for (const c of clinics) {
          const li = document.createElement('li');
          const lvl2 = (c.health_level || 'green').toLowerCase();
          li.className = 'hc-row hc-row-' + lvl2;
          const bullet = document.createElement('span');
          bullet.className = 'hc-bullet hc-bullet-' + lvl2;
          li.appendChild(bullet);
          const body = document.createElement('div');
          body.className = 'min-w-0 flex-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5';
          const name = document.createElement('span');
          name.className = 'font-medium text-[#E5E7EB]';
          name.textContent = c.clinic_name || ('JID ' + c.jid);
          const stats = document.createElement('span');
          stats.className = 'font-mono tabular-nums text-xs text-[#9CA3AF]';
          stats.textContent =
            (c.error_rate_24h != null ? Number(c.error_rate_24h).toFixed(1) + '% err' : '—')
            + ' · ' + fmtNum(c.facts_24h) + ' / ' + fmtNum(c.pending_now) + ' · ' + fmtIsoShort(c.last_seen_at);
          body.appendChild(name);
          body.appendChild(stats);
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
        { label: 'max(last_egmid, source_max_egmid)', value: fmtNum(p.etl_cursor_egmid) },
        { label: 'Лаг пика etl_state vs staging', value: fmtNum(p.egmid_lag) },
        { label: 'last_log_id', value: fmtNum(p.etl_last_log_id) },
      ];
      if (p.fact_rows != null) items.push({ label: 'Витрина: строк', value: fmtNum(p.fact_rows) });
      if (p.fact_without_egmid != null) items.push({ label: 'Витрина: без EGMID', value: fmtNum(p.fact_without_egmid) });
      if (p.fact_max_egmid != null) items.push({ label: 'Витрина: max EGMID', value: fmtNum(p.fact_max_egmid) });
      for (const it of items) {
        const li = document.createElement('li');
        li.className = 'flex items-baseline justify-between gap-2 min-w-0';
        const lab = document.createElement('span');
        lab.className = 'truncate text-[#9CA3AF] text-sm';
        lab.textContent = it.label;
        const v = document.createElement('span');
        v.className = 'shrink-0 font-mono tabular-nums text-sm text-[#E5E7EB]';
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
  const POLL_FAIL = { sync: 0, snap: 0, hc: 0 };
  /** Пока идёт POST /api/sync/start — pollSync не перезаписывает #syncStatus (иначе тик 1.5 с затирает «Запрос…» и кажется, что клик ничего не сделал). */
  var syncStartInFlight = false;
  function isFetchTransportError(e) {
    if (!e) return false;
    const name = e && e.name ? String(e.name) : '';
    const msg = e && e.message ? String(e.message) : String(e);
    return name === 'TypeError' && /fetch|network|load failed/i.test(msg);
  }
  function syncActionButtonsFromPoll(j) {
    var btn = document.getElementById('btnSync');
    var stopBtn = document.getElementById('btnSyncStop');
    if (!btn || !stopBtn) return;
    var running = !!(j && j.running);
    btn.disabled = !!(syncStartInFlight || running);
    stopBtn.disabled = !running || !!syncStartInFlight;
  }
  async function pollSync() {
    const el = document.getElementById('syncStatus');
    if (!el) return;
    try {
      const r = await fetch('/api/sync/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      POLL_FAIL.sync = 0;
      const prevRun = wasSyncRunning;
      wasSyncRunning = !!j.running;
      if (j.running && !prevRun) {
        lastUiMessage = { ok: true, strip: '', logBody: '' };
      }
      if (prevRun && !wasSyncRunning) {
        loadPgSyncSnapshotOnce();
      }
      lastSyncJson = j;
      refreshConnStatusStrip();
      syncActionButtonsFromPoll(j);
      applySyncStatusFromPoll(j, el);
    } catch (e) {
      POLL_FAIL.sync += 1;
      if (POLL_FAIL.sync <= 2) return;
      const transport = isFetchTransportError(e);
      if (el && !syncStartInFlight) {
        if (transport && lastSyncJson && lastSyncJson.running) {
          applySyncStatusFromPoll(lastSyncJson, el);
          return;
        }
        const reason = transport
          ? 'Нет связи с conf-ui'
          : ('Ошибка опроса: ' + (e && e.message ? e.message : e));
        el.textContent = reason;
        el.setAttribute('data-raw', reason);
      }
    }
  }
  function showCfgMessage(ok, text, opt) {
    const o = opt || {};
    const logBody = o.logBody != null ? o.logBody : (text != null ? String(text) : '');
    var strip;
    if (o.strip === undefined || o.strip === null) {
      strip = ok ? '' : 'Ошибка';
    } else {
      strip = String(o.strip).trim();
    }
    lastUiMessage = { ok: !!ok, strip: strip, logBody: String(logBody) };
    refreshConnStatusStrip();
    const elLog = document.getElementById('syncStatus');
    if (elLog && !syncStartInFlight) {
      applySyncStatusFromPoll(lastSyncJson, elLog);
    }
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
      showCfgMessage(false, 'Ответ сервера не JSON (код ' + r.status + '). ' + raw.slice(0, 400), {
        strip: 'Ошибка ответа',
        logBody: 'Ответ сервера не JSON (код ' + r.status + '). ' + raw.slice(0, 400),
      });
      return;
    }
    const msg = j.message != null ? String(j.message) : (j.ok ? 'OK' : 'Ошибка');
    var strip = '';
    if (url === '/test-fb') {
      strip = j.ok ? 'Подключение к Firebird OK' : 'Ошибка Firebird';
    } else if (url === '/test-pg') {
      strip = j.ok ? 'Подключение к PostgreSQL OK' : 'Ошибка PostgreSQL';
    } else if (url === '/save') {
      strip = j.ok ? 'Сохранено' : 'Ошибка записи';
    } else {
      strip = j.ok ? 'Готово' : 'Ошибка';
    }
    var displayMsg = msg;
    if (j.ok && (url === '/test-fb' || url === '/test-pg')) displayMsg = strip;
    showCfgMessage(!!j.ok, displayMsg, { strip: strip, logBody: displayMsg });
  }
  function bindClick(id, fn) {
    var elBind = document.getElementById(id);
    if (elBind) elBind.addEventListener('click', fn);
    else if (typeof console !== 'undefined' && console.warn) console.warn('[config-ui] missing #' + id);
  }
  const RIGHT_ASIDE_KEY = 'egisz_conf_ui_right_aside_collapsed';
  function readAsideCollapsed() {
    try {
      return localStorage.getItem(RIGHT_ASIDE_KEY) === '1';
    } catch (e) {
      return false;
    }
  }
  function writeAsideCollapsed(v) {
    try {
      localStorage.setItem(RIGHT_ASIDE_KEY, v ? '1' : '0');
    } catch (e) {}
  }
  function applyRightAsideCollapsed(collapsed) {
    const aside = document.getElementById('rightAsideShell');
    const btn = document.getElementById('btnRightAsideToggle');
    if (!aside || !btn) return;
    if (collapsed) {
      aside.classList.add('hidden', 'lg:hidden');
      btn.textContent = 'Показать панель';
    } else {
      aside.classList.remove('hidden', 'lg:hidden');
      btn.textContent = 'Скрыть панель';
    }
  }
  bindClick('btnSaveYaml', async function () {
    if (!confirm('Сохранить текущую конфигурацию из полей формы в YAML на сервере?')) return;
    try {
      await postConfigForm('/save');
    } catch (e) {
      showCfgMessage(false, String(e), { strip: 'Ошибка', logBody: String(e) });
    }
  });
  bindClick('btnTestFb', async function () {
    try {
      await postConfigForm('/test-fb');
    } catch (e) {
      showCfgMessage(false, String(e), { strip: 'Ошибка', logBody: String(e) });
    }
  });
  bindClick('btnTestPg', async function () {
    try {
      await postConfigForm('/test-pg');
    } catch (e) {
      showCfgMessage(false, String(e), { strip: 'Ошибка', logBody: String(e) });
    }
  });
  bindClick('btnMbExportDir', async function () {
    if (typeof window.showDirectoryPicker !== 'function') {
      showCfgMessage(false, 'Выбор папки для ZIP поддерживается в Chromium (Chrome, Edge, Яндекс.Браузер). Иначе архив сохранится через загрузки браузера.', {
        strip: 'Metabase',
        logBody: 'showDirectoryPicker недоступен',
      });
      return;
    }
    try {
      mbExportDirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
      const hint = document.getElementById('mbExportDirHint');
      if (hint) {
        hint.textContent = 'ZIP Metabase: ' + (mbExportDirHandle.name || 'папка');
        hint.classList.remove('hidden');
        hint.title = hint.textContent;
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return;
      showCfgMessage(false, String(e && e.message ? e.message : e), { strip: 'Metabase', logBody: String(e) });
    }
  });
  bindClick('btnPgBackupDir', async function () {
    if (typeof window.showDirectoryPicker !== 'function') {
      showCfgMessage(false, 'Выбор папки для сохранения поддерживается в Chromium (Chrome, Edge, Яндекс.Браузер). Иначе дамп уйдёт в папку загрузок по умолчанию.', {
        strip: 'Папка',
        logBody: 'showDirectoryPicker недоступен',
      });
      return;
    }
    try {
      pgBackupDirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
      const hint = document.getElementById('pgBackupDirHint');
      if (hint) {
        hint.textContent = 'Сохранение: ' + (pgBackupDirHandle.name || 'папка');
        hint.classList.remove('hidden');
        hint.title = hint.textContent;
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return;
      showCfgMessage(false, String(e && e.message ? e.message : e), { strip: 'Папка', logBody: String(e) });
    }
  });
  bindClick('btnPgRestorePick', function () {
    const inp = document.getElementById('pgRestoreFile');
    if (inp) inp.click();
  });
  (function bindPgRestoreFileHint() {
    const inp = document.getElementById('pgRestoreFile');
    if (!inp) return;
    inp.addEventListener('change', function () {
      const hint = document.getElementById('pgRestoreFileHint');
      const f = inp.files && inp.files[0];
      if (!hint) return;
      if (f) {
        hint.textContent = 'Файл: ' + f.name;
        hint.classList.remove('hidden');
        hint.title = f.name;
      } else {
        hint.textContent = '';
        hint.classList.add('hidden');
        hint.title = '';
      }
    });
  })();
  bindClick('btnMbExport', async function () {
    let r;
    try {
      r = await fetch('/api/metabase/export-dashboards-json', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { Accept: 'application/zip' },
      });
    } catch (e) {
      showCfgMessage(false, String(e), { strip: 'Metabase', logBody: String(e) });
      return;
    }
    if (!r.ok) {
      const raw = await r.text();
      let j;
      try {
        j = JSON.parse(raw);
      } catch (e2) {
        showCfgMessage(false, 'Metabase export: код ' + r.status + '. ' + raw.slice(0, 400), { strip: 'Ошибка', logBody: raw });
        return;
      }
      const errMsg = (j && j.message) || raw.slice(0, 400);
      showCfgMessage(false, errMsg, { strip: 'Metabase export', logBody: errMsg });
      return;
    }
    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    const srcHdr = (r.headers.get('X-Egisz-Metabase-Export-Source') || '').trim().toLowerCase();
    const fromBundled = srcHdr === 'bundled';
    let name = 'egisz_metabase_dashboards.zip';
    const m = cd.match(/filename="([^"]+)"/i);
    if (m && m[1]) {
      try {
        name = decodeURIComponent(m[1].replace(/\"/g, '').trim());
      } catch (e3) {
        name = m[1].replace(/\"/g, '').trim() || name;
      }
    }
    if (mbExportDirHandle && typeof mbExportDirHandle.getFileHandle === 'function') {
      try {
        const fh = await mbExportDirHandle.getFileHandle(name, { create: true });
        const w = await fh.createWritable();
        await w.write(blob);
        await w.close();
        showCfgMessage(
          true,
          fromBundled
            ? 'Скачан эталонный ZIP из образа (metabase_dashboards/). Живая выгрузка Metabase недоступна — проверьте Secret или api_key.'
            : 'Архив JSON из Metabase записан в выбранную папку.',
          { strip: 'Metabase', logBody: 'ZIP: ' + name + (fromBundled ? ' (bundled)' : ' (live)') },
        );
        return;
      } catch (e) {
        showCfgMessage(false, String(e && e.message ? e.message : e), { strip: 'Metabase', logBody: String(e) });
        return;
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
    showCfgMessage(
      true,
      fromBundled
        ? 'Скачан эталонный ZIP из образа (metabase_dashboards/). Для выгрузки с живого Metabase задайте api_key или пароль в Secret.'
        : 'Архив JSON из Metabase скачан.',
      { strip: 'Metabase', logBody: 'ZIP: ' + name + (fromBundled ? ' (bundled)' : ' (live)') },
    );
  });
  bindClick('btnPgBackup', async function () {
    const fd = new FormData(document.getElementById('configForm'));
    let r;
    try {
      r = await fetch('/api/pg/backup', {
        method: 'POST',
        body: fd,
        credentials: 'same-origin',
      });
    } catch (e) {
      showCfgMessage(false, String(e), { strip: 'Ошибка', logBody: String(e) });
      return;
    }
    if (!r.ok) {
      const raw = await r.text();
      let j;
      try {
        j = JSON.parse(raw);
      } catch (e2) {
        showCfgMessage(false, 'Бэкап: код ' + r.status + '. ' + raw.slice(0, 400), { strip: 'Ошибка бэкапа', logBody: 'Бэкап: код ' + r.status + '. ' + raw.slice(0, 400) });
        return;
      }
      const errMsg = (j && j.message) || raw.slice(0, 400);
      showCfgMessage(false, errMsg, { strip: 'Ошибка бэкапа', logBody: errMsg });
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
    showCfgMessage(true, 'Бэкап скачан.', { strip: 'Бэкап', logBody: 'Бэкап скачан.' });
  });
  bindClick('btnPgRestore', async function () {
    const inp = document.getElementById('pgRestoreFile');
    if (!inp || !inp.files || !inp.files[0]) {
      if (inp) inp.click();
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
      showCfgMessage(false, String(e), { strip: 'Ошибка', logBody: String(e) });
      return;
    }
    const raw = await r.text();
    let j;
    try {
      j = JSON.parse(raw);
    } catch (e2) {
      showCfgMessage(false, 'Ответ сервера не JSON (код ' + r.status + '). ' + raw.slice(0, 400), { strip: 'Ошибка', logBody: raw.slice(0, 400) });
      return;
    }
    const msgR = j.message != null ? String(j.message) : (j.ok ? 'OK' : 'Ошибка');
    showCfgMessage(!!j.ok, msgR, {
      strip: j.ok ? 'Восстановление' : 'Ошибка восстановления',
      logBody: msgR,
    });
    if (j.ok && inp) inp.value = '';
  });
  applyRightAsideCollapsed(readAsideCollapsed());
  bindClick('btnRightAsideToggle', function () {
    const aside = document.getElementById('rightAsideShell');
    const collapsed = !!(aside && aside.classList.contains('hidden'));
    writeAsideCollapsed(!collapsed);
    applyRightAsideCollapsed(!collapsed);
  });
  var btnSyncEl = document.getElementById('btnSync');
  var btnSyncDefaultLabel = 'Запустить синхронизацию';
  if (btnSyncEl && btnSyncEl.getAttribute('data-default-label')) {
    btnSyncDefaultLabel = btnSyncEl.getAttribute('data-default-label');
  }
  bindClick('btnSync', async function() {
    const el = document.getElementById('syncStatus');
    const form = document.getElementById('configForm');
    if (!el) return;
    if (!form) {
      el.textContent = 'В DOM нет формы #configForm — обновите страницу (кэш/прокси могли отдать неполный HTML).';
      el.setAttribute('data-raw', el.textContent);
      return;
    }
    if (btnSyncEl && btnSyncEl.disabled) return;
    lastUiMessage = { ok: true, strip: '', logBody: '' };
    syncStartInFlight = true;
    if (btnSyncEl) {
      btnSyncEl.disabled = true;
      btnSyncEl.setAttribute('aria-busy', 'true');
      btnSyncEl.textContent = 'Запуск...';
    }
    try {
      el.textContent = 'Запрос...';
      el.setAttribute('data-raw', el.textContent);
      const fd = new FormData(form);
      var ac = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var to = ac ? setTimeout(function () { ac.abort(); }, 45000) : null;
      let r;
      try {
        r = await fetch('/api/sync/start', {
          method: 'POST',
          body: fd,
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
          signal: ac ? ac.signal : undefined,
        });
      } catch (e) {
        if (to) clearTimeout(to);
        const name = e && e.name ? String(e.name) : '';
        if (name === 'AbortError') {
          el.textContent = 'Таймаут запроса (45 с): нет ответа от conf-ui (rollout, перегрузка или обрыв сети). Обновите страницу или проверьте kubectl/port-forward.';
        } else {
          el.textContent = String(e);
        }
        el.setAttribute('data-raw', el.textContent);
        return;
      }
      if (to) clearTimeout(to);
      const raw = await r.text();
      if (!r.ok) {
        el.textContent = 'Ошибка HTTP ' + r.status + String.fromCharCode(10) + raw.slice(0, 800);
        el.setAttribute('data-raw', el.textContent);
        await pollSync();
        return;
      }
      let j;
      try {
        j = JSON.parse(raw);
      } catch (e2) {
        el.textContent = 'Ответ сервера не JSON (код ' + r.status + '). ' + raw.slice(0, 400);
        el.setAttribute('data-raw', el.textContent);
        return;
      }
      el.textContent = j.message || j.error || JSON.stringify(j);
      el.setAttribute('data-raw', el.textContent);
      await pollSync();
    } finally {
      syncStartInFlight = false;
      if (btnSyncEl) {
        btnSyncEl.removeAttribute('aria-busy');
        btnSyncEl.textContent = btnSyncDefaultLabel;
      }
      syncActionButtonsFromPoll(lastSyncJson);
    }
  });
  bindClick('btnSyncStop', async function () {
    try {
      const r = await fetch('/api/sync/stop', {
        method: 'POST',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      const raw = await r.text();
      let j;
      try {
        j = JSON.parse(raw);
      } catch (e) {
        return;
      }
      const msg = j.message != null ? String(j.message) : '';
      showCfgMessage(!!j.ok, msg || (j.ok ? 'OK' : 'Ошибка'), {
        strip: j.ok ? 'Стоп' : 'Стоп',
        logBody: msg,
      });
      await pollSync();
    } catch (e) {
      showCfgMessage(false, String(e), { strip: 'Стоп', logBody: String(e) });
    }
  });
  // Опрос только пока вкладка видима. Скрытая вкладка через 5–10 минут sleep'а
  // получала залп fetch'ей разом и стабильно ловила TypeError: Failed to fetch.
  let fastTimer = null;
  let snapTimer = null;
  let slowTimer = null;
  function startPolling() {
    if (fastTimer == null) {
      fastTimer = setInterval(function () {
        pollSync();
      }, 1500);
    }
    if (snapTimer == null) {
      snapTimer = setInterval(function () {
        loadPgSyncSnapshotOnce();
      }, 10000);
    }
    if (slowTimer == null) {
      slowTimer = setInterval(function () {
        pollHealthcheck();
      }, 30000);
    }
  }
  function stopPolling() {
    if (fastTimer != null) { clearInterval(fastTimer); fastTimer = null; }
    if (snapTimer != null) { clearInterval(snapTimer); snapTimer = null; }
    if (slowTimer != null) { clearInterval(slowTimer); slowTimer = null; }
  }
  startPolling();
  pollSync();
  refreshConnStatusStrip();
  syncActionButtonsFromPoll(lastSyncJson);
  setTimeout(function () { loadPgSyncSnapshotOnce(); }, 40);
  setTimeout(function () { pollHealthcheck(); }, 120);
  window.addEventListener('pageshow', function () {
    pollSync();
    refreshConnStatusStrip();
    setTimeout(function () { loadPgSyncSnapshotOnce(); }, 40);
    setTimeout(function () { pollHealthcheck(); }, 120);
  });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      startPolling();
      pollSync();
      setTimeout(function () { loadPgSyncSnapshotOnce(); }, 40);
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

    def _ensure_dict(root: dict[str, Any], key: str) -> dict[str, Any]:
        cur = root.get(key)
        if not isinstance(cur, dict):
            cur = {}
            root[key] = cur
        return cur

    old: dict[str, Any] = {}
    if p.is_file():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            old = loaded
    _ensure_dict(old, "firebird")
    _ensure_dict(old, "postgres")
    _ensure_dict(old, "etl")
    mb_cur = old.get("metabase")
    old["metabase"] = dict(mb_cur) if isinstance(mb_cur, dict) else {}
    _ensure_dict(old, "auto_sync")

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
    old["etl"].pop("messages_snapshot_full_refresh", None)

    old["auto_sync"]["enabled"] = bool(form.get("auto_sync_enabled"))
    cron_in = str(form.get("auto_sync_schedule_cron", "") or "").strip()
    if cron_in:
        old["auto_sync"]["schedule_cron"] = cron_in
    tz_in = str(form.get("auto_sync_timezone", "") or "").strip()
    if tz_in:
        old["auto_sync"]["timezone"] = tz_in
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
        html = render_template_string(
            PAGE,
            path=str(logical_config_path()),
            fb=cfg.firebird,
            pg=cfg.postgres,
            etl=cfg.etl,
            auto_sync=cfg.auto_sync,
            metabase_site_url=mb_url,
        )
        resp = make_response(html)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

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
        cron_ok, cron_detail = reconcile_egisz_monitor_sync_cronjob(merged.get("auto_sync") or {})
        etl_sec = merged.get("etl") if isinstance(merged.get("etl"), dict) else {}
        bs = etl_sec.get("batch_size")
        sw = etl_sec.get("sync_window_days")
        base = f"Сохранено. ETL: batch_size={bs}, sync_window_days={sw}."
        if cron_ok:
            msg = f"{base} {cron_detail}"
        else:
            msg = f"{base} Автосинк (CronJob): не удалось применить — {cron_detail}"
        return jsonify(
            {
                "ok": True,
                "message": msg,
                "cronjob_reconcile": {"ok": cron_ok, "detail": cron_detail},
            }
        )

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

    @app.post("/api/metabase/export-dashboards-json")
    def api_metabase_export_dashboards_json():  # type: ignore[no-untyped-def]
        """ZIP с JSON дашбордов: сначала живой Metabase; при ошибке — эталон из образа (metabase_dashboards/)."""
        try:
            blob, fn, source = build_export_zip_bytes()
        except RuntimeError as e:
            return jsonify({"ok": False, "message": str(e)}), 400
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "message": f"Metabase export: {e}"}), 502
        return Response(
            blob,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{fn}"',
                "X-Egisz-Metabase-Export-Source": source,
            },
        )

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

        # Опрос Firebird — только MAX(MODIFYDATE) по лицензиям (timeout); пик EGMID в proxy_db — из PG healthcheck (etl_state).
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout

        fb_lic: dict[str, Any] = {"max_licenses_modifydate": None, "error": None}
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(fetch_firebird_max_license_modifydate, cfg.firebird)
            try:
                fb_lic = fut.result(timeout=20.0)
            except _FutureTimeout:
                fb_lic = {"max_licenses_modifydate": None, "error": "timeout"}
            except Exception as e:  # pragma: no cover
                fb_lic = {"max_licenses_modifydate": None, "error": str(e)}
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        if fb_lic.get("max_licenses_modifydate") is None and pg_cached_peaks.get(
            "source_max_licenses_modifydate"
        ):
            fb_lic["max_licenses_modifydate"] = pg_cached_peaks["source_max_licenses_modifydate"]

        peaks_resolved = bool(fb_lic.get("max_licenses_modifydate"))

        proxy_db = result.get("proxy_db") or {}
        ec = proxy_db.get("etl_cursor_egmid")
        st = proxy_db.get("staging_max_egmid")
        if isinstance(ec, int) and isinstance(st, int):
            proxy_db["egmid_lag"] = max(0, ec - st)

        if fb_lic.get("max_licenses_modifydate"):
            proxy_db["fb_max_licenses_modifydate"] = fb_lic["max_licenses_modifydate"]
        if fb_lic.get("error") and not peaks_resolved:
            result.setdefault("errors", []).append(f"Firebird licenses peak: {fb_lic['error']}")
        result["proxy_db"] = proxy_db

        return jsonify(result)

    from egisz_monitor_corp.sync_routes import register_sync_routes

    register_sync_routes(app, config_path, _merged_yaml_dict_from_form)

    return app


def run_dev() -> None:
    import os
    import sys

    app = create_app()
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "8765"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    use_reloader = debug or os.environ.get("CONFIG_UI_RELOAD") == "1"
    if not use_reloader:
        print(
            "config-ui: изменения в egisz_monitor_corp/config_app.py видны только после перезапуска процесса. "
            "Локально задайте CONFIG_UI_RELOAD=1 или FLASK_DEBUG=1. "
            "Образ Docker/gunicorn на :8080 — пересоберите образ и перезапустите контейнер.",
            file=sys.stderr,
        )
    app.run(host=host, port=port, debug=debug, use_reloader=use_reloader, threaded=True)
