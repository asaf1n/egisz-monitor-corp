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
from egisz_monitor_corp.pg_warehouse import test_pg_connection

PAGE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <title>EGISZ Corp — конфигурация</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    /* ~50px tall inputs; card max-width keeps host/DB lines from stretching across ultra-wide monitors */
    body { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    .cfg-in {
      min-height: 3.125rem;
      padding-top: 0.625rem;
      padding-bottom: 0.625rem;
      padding-left: 0.8125rem;
      padding-right: 0.8125rem;
    }
    pre::-webkit-scrollbar { width: 8px; height: 8px; }
    pre::-webkit-scrollbar-track { background: #0F1522; }
    pre::-webkit-scrollbar-thumb { background: #2D3F5E; border-radius: 4px; }
    pre::-webkit-scrollbar-thumb:hover { background: #3E5A85; }
    #syncProgressBar.sync-progress-indeterminate {
      width: 32% !important;
      animation: sync-bar-pulse 1.05s ease-in-out infinite alternate;
    }
    @keyframes sync-bar-pulse {
      from { opacity: 0.4; }
      to { opacity: 1; }
    }
  </style>
</head>
<body class="min-h-screen bg-[#121826] text-white">
  <div class="w-full max-w-[52rem] mx-auto px-4 sm:px-5 py-3 sm:py-4">
    <nav class="flex items-center justify-center gap-3 text-xs mb-3">
      <span class="text-[#509EE3]">Конфигурация</span>
      <span class="text-[#4B5563]">|</span>
      <a href="/" class="text-[#4B5563] transition hover:text-[#509EE3]">Обновить страницу</a>
    </nav>

    <section class="rounded-xl bg-[#0F1522] px-4 py-4 sm:px-5 shadow-lg border border-[#1B2940]">
      <div class="mb-4 border-b border-[#1B2940] pb-3">
        <h1 class="text-base sm:text-lg font-semibold text-white">Конфигурация БД (EGISZ Monitor Corp)</h1>
        <p class="mt-0.5 text-xs sm:text-sm text-[#9CA3AF]">
          Файл: <code class="text-[#509EE3]">{{ path }}</code><br/>
          Переопределение: переменная окружения <code class="text-[#509EE3]">EGISZ_CORP_CONFIG</code> или <code class="text-[#509EE3]">CONFIG_WRITE_PATH</code>.
        </p>
      </div>

      <div id="cfgMessageWrap" class="mb-6 hidden rounded-md p-4 border" role="status">
        <div id="cfgMessageText" class="whitespace-pre-wrap"></div>
      </div>

      <form id="configForm" class="space-y-4" action="#" onsubmit="return false;">

        <div class="grid grid-cols-1 gap-5 lg:grid-cols-2 lg:gap-x-6 lg:gap-y-0 lg:items-stretch">
        <!-- Firebird Section -->
        <div class="min-w-0 lg:pr-5 lg:border-r lg:border-[#1B2940]">
          <div class="mb-2">
            <h2 class="text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">Firebird Configuration</h2>
            <p class="mt-0.5 text-[11px] sm:text-xs text-[#9CA3AF] leading-snug">TCP к серверу Firebird (как в DBeaver). Если в Docker — используйте <code class="text-white">host.docker.internal</code></p>
          </div>

          <div class="grid gap-2.5 grid-cols-1 sm:grid-cols-[minmax(0,1fr)_5.5rem] sm:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">host</span>
              <input name="fb_host" value="{{ fb.host }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full sm:w-[5.5rem] sm:max-w-[5.5rem] sm:justify-self-start">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">port</span>
              <input name="fb_port" type="number" value="{{ fb.port }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid gap-2.5 grid-cols-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,10.5rem)] sm:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">database (alias/path)</span>
              <input name="fb_database" value="{{ fb.database }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
              <p class="mt-1 text-[10px] text-[#6B7280] leading-snug">Точно как на сервере Firebird: имя из <code class="text-[#9CA3AF]">aliases.conf</code> или полный путь к .fdb. Если в логе <code class="text-[#9CA3AF]">CreateFile … «имя»</code> — сервер не нашёл алиас/файл (часто путают <code class="text-[#9CA3AF]">proxy_egisz</code> и <code class="text-[#9CA3AF]">proxy-egisz</code>).</p>
            </label>
            <label class="block w-full sm:w-[10.5rem] sm:max-w-[10.5rem] sm:justify-self-start">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">user</span>
              <input name="fb_user" value="{{ fb.user }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid gap-2.5 grid-cols-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,7.25rem)] sm:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">password</span>
              <input name="fb_password" type="password" value="{{ fb.password }}" autocomplete="current-password" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full sm:w-[7.25rem] sm:max-w-[7.25rem] sm:justify-self-start">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">charset</span>
              <input name="fb_charset" value="{{ fb.charset }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
        </div>

        <!-- PostgreSQL Section -->
        <div class="min-w-0 pt-4 border-t border-[#1B2940] lg:pt-0 lg:pl-5 lg:border-0">
          <div class="mb-2">
            <h2 class="text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">PostgreSQL Configuration</h2>
            <p class="mt-0.5 text-[11px] sm:text-xs text-[#9CA3AF] leading-snug">Из пода витрины обычно <code class="text-white">postgres.egisz-corp.svc.cluster.local:5432</code></p>
          </div>

          <div class="grid gap-2.5 grid-cols-1 sm:grid-cols-[minmax(0,1fr)_5.5rem] sm:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">host</span>
              <input name="pg_host" value="{{ pg.host }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full sm:w-[5.5rem] sm:max-w-[5.5rem] sm:justify-self-start">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">port</span>
              <input name="pg_port" type="number" value="{{ pg.port }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid gap-2.5 grid-cols-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,9.5rem)] sm:items-end">
            <label class="block min-w-0">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">database</span>
              <input name="pg_database" value="{{ pg.database }}" required class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
            <label class="block w-full sm:w-[9.5rem] sm:max-w-[9.5rem] sm:justify-self-start">
              <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">schema</span>
              <input name="pg_schema" value="{{ pg.schema }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
            </label>
          </div>
          <div class="mt-2.5 grid gap-2.5 grid-cols-1 sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)] sm:items-end">
            <label class="block w-full sm:w-[11rem] sm:max-w-[11rem] sm:justify-self-start">
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

        <div class="pt-3 flex flex-wrap gap-2 border-t border-[#1B2940]">
          <button type="button" id="btnSaveYaml" class="inline-flex min-h-[2.875rem] min-w-[140px] items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white">
            Сохранить в YAML
          </button>
          <button type="button" id="btnTestFb" class="inline-flex min-h-[2.875rem] min-w-[140px] items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white">
            Проверить Firebird
          </button>
          <button type="button" id="btnTestPg" class="inline-flex min-h-[2.875rem] min-w-[140px] items-center justify-center rounded-md border border-[#2D3F5E] bg-[#1B2940] px-3.5 py-2.5 font-mono text-sm text-[#D1D5DB] transition hover:border-[#3E5A85] hover:bg-[#223555] hover:text-white">
            Проверить PostgreSQL
          </button>
        </div>

        <!-- ETL + sync trigger: fields left, button right on large screens -->
        <div class="pt-3 border-t border-[#1B2940] flex flex-col gap-4 lg:flex-row lg:items-stretch lg:gap-6">
          <div class="min-w-0 flex-1">
            <div class="mb-2">
              <h2 class="text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">ETL Configuration</h2>
            </div>
            <div class="grid gap-2.5 grid-cols-1 sm:grid-cols-[minmax(0,6.5rem)_minmax(0,7.5rem)_minmax(0,1fr)] sm:items-end">
              <label class="block w-full max-w-[6.5rem] sm:max-w-none">
                <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">batch_size</span>
                <input name="etl_batch" type="number" value="{{ etl.batch_size }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm tabular-nums text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
              </label>
              <label class="block w-full max-w-[7.5rem] sm:max-w-none">
                <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">sync_window_days</span>
                <input name="etl_sync_days" type="number" value="{{ etl.sync_window_days }}" class="cfg-in mt-1.5 w-full rounded-lg bg-[#121826] border border-[#1B2940] font-mono text-sm tabular-nums text-white outline-none transition focus:border-[#509EE3] focus:ring-1 focus:ring-[#509EE3]"/>
              </label>
              <label class="block flex flex-col justify-end pb-0.5">
                <span class="font-mono text-[11px] uppercase tracking-[0.16em] text-[#4B5563] mb-1.5">full_scan</span>
                <div class="flex items-center min-h-[3.125rem] sm:min-h-0 sm:items-center sm:pb-1.5">
                  <input type="checkbox" name="etl_full_scan" value="1" {{ 'checked' if etl.full_scan else '' }} class="h-4 w-4 rounded border-[#1B2940] bg-[#121826] text-[#509EE3] focus:ring-[#509EE3] focus:ring-offset-[#0F1522]"/>
                  <span class="ml-2 text-xs text-[#9CA3AF]">Включить полный скан</span>
                </div>
              </label>
            </div>
          </div>
          <div class="lg:flex-shrink-0 lg:border-l lg:border-[#1B2940] lg:pl-6 flex flex-col justify-start gap-2 min-w-[12rem]">
            <h2 class="text-[11px] uppercase tracking-[0.16em] text-[#4B5563]">Синхронизация Firebird -&gt; PostgreSQL</h2>
            <p class="text-[11px] text-[#9CA3AF] leading-snug">Полный цикл ETL в фоне. Не закрывайте вкладку до завершения.</p>
            <button type="button" id="btnSync" class="inline-flex w-full sm:w-fit min-h-[2.875rem] min-w-[200px] items-center justify-center rounded-md border border-[#F59F36] bg-[#F59F36] px-3.5 py-2.5 font-mono text-sm text-[#121826] transition hover:bg-[#FFB95D]">
              Запустить синхронизацию
            </button>
          </div>
        </div>
      </form>

      <div class="mt-4 pt-3 border-t border-[#1B2940]">
        <div class="grid grid-cols-1 gap-2 lg:grid-cols-2 lg:gap-3">
          <div class="flex flex-col gap-2 min-w-0">
            <div id="syncProgressWrap" class="hidden rounded-md bg-[#0B1120] px-3 py-2 border border-[#1B2940]">
              <div class="flex justify-between items-baseline gap-2 mb-1.5">
                <span id="syncProgressLabel" class="text-[10px] uppercase tracking-[0.16em] text-[#509EE3]">прогресс</span>
                <span id="syncProgressFraction" class="font-mono text-[11px] text-[#D1D5DB]"></span>
              </div>
              <div class="h-2 w-full rounded-full bg-[#1B2940] overflow-hidden">
                <div id="syncProgressBar" class="h-full rounded-full bg-[#509EE3] transition-[width] duration-300 ease-out" style="width:0%"></div>
              </div>
              <p id="syncProgressMeta" class="mt-1.5 whitespace-pre-line font-mono text-[10px] text-[#9CA3AF] leading-relaxed"></p>
            </div>
          </div>
          <div class="rounded-md bg-[#0B1120] px-3 py-2 text-sm text-[#93A1B6] border border-[#1B2940] min-h-0 min-w-0">
            <div class="mb-1 text-[10px] uppercase tracking-[0.16em] text-[#509EE3]">system log</div>
            <pre id="syncStatus" class="whitespace-pre-wrap font-mono text-[11px] text-[#D1D5DB] min-h-[2.25rem] max-h-28 overflow-y-auto leading-relaxed"></pre>
          </div>
        </div>
      </div>
    </section>
  </div>

  <script>
  const PHASE_RU = {
    counting: 'Подсчёт строк в Firebird…',
    exchangelog_ready: 'К журналу EXCHANGELOG',
    fetch_page: 'Загрузка страницы из Firebird…',
    parsing: 'Парсинг SOAP / журнал…',
    page_done: 'Сохранение страницы в PostgreSQL…',
    exchangelog_done: 'Журнал обработан',
    outbound_firebird: 'Исходящие: чтение Firebird…',
    outbound_fetch: 'Исходящие сообщения (EGISZ_MESSAGES)…',
    outbound_parse: 'Разбор исходящих…',
    outbound_postgres: 'Исходящие: запись в PostgreSQL…',
    outbound_done: 'Исходящие: готово',
  };
  function setBarIndeterminate(bar, on) {
    if (on) bar.classList.add('sync-progress-indeterminate');
    else bar.classList.remove('sync-progress-indeterminate');
  }
  function renderProgress(j) {
    const wrap = document.getElementById('syncProgressWrap');
    const bar = document.getElementById('syncProgressBar');
    const frac = document.getElementById('syncProgressFraction');
    const label = document.getElementById('syncProgressLabel');
    const meta = document.getElementById('syncProgressMeta');
    const p = j.progress;

    if (!j.running) {
      wrap.classList.add('hidden');
      setBarIndeterminate(bar, false);
      return;
    }

    wrap.classList.remove('hidden');

    if (!p || typeof p !== 'object') {
      label.textContent = 'ПОДГОТОВКА';
      bar.style.width = '';
      setBarIndeterminate(bar, true);
      frac.textContent = '…';
      meta.textContent =
        'Запуск пайплайна: конфигурация, курсор ETL, справочники из Firebird…' +
        String.fromCharCode(10) +
        'Детальный прогресс появится после подсчёта объёма журнала.';
      return;
    }

    const phase = p.phase || '';

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
      const j = Number(pp.journal_facts);
      if (Number.isFinite(j) && j >= 0) return j;
      return Number(pp.parsed_facts) || 0;
    }

    if (phase === 'outbound_firebird') {
      label.textContent = (PHASE_RU[phase] || phase || 'прогресс').toUpperCase();
      bar.style.width = '';
      setBarIndeterminate(bar, true);
      frac.textContent = '…';
      const tr = totalRowsState(p);
      const jlo = Number(p.loaded_rows) || 0;
      let lineA = 'Чтение EGISZ_MESSAGES из Firebird (окно sync_window_days). Пока идёт запрос, факты журнала уже в витрине.';
      let lineB = 'Фактов в fact_egisz_transactions: ' + journalFacts(p) + ' · staging ошибок (журнал): ' + (Number(p.staging_errors) || 0);
      if (tr.kind === 'positive') lineB += ' · журнал: ' + jlo + ' / ' + tr.n + ' строк';
      else if (tr.kind === 'zero') lineB += ' · журнал: новых строк не было';
      meta.textContent = lineA + String.fromCharCode(10) + lineB;
      return;
    }

    setBarIndeterminate(bar, false);

    label.textContent = (PHASE_RU[phase] || phase || 'прогресс').toUpperCase();
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

    bar.style.width = pct + '%';
    frac.textContent = pct + '%';
    meta.textContent = lineA + String.fromCharCode(10) + lineB;
  }
  async function pollSync() {
    const el = document.getElementById('syncStatus');
    try {
      const r = await fetch('/api/sync/status');
      const j = await r.json();
      renderProgress(j);
      const parts = [j.message || '', j.running ? 'Статус: выполняется' : 'Статус: ожидание'];
      if (j.error) parts.push('Ошибка: ' + j.error);
      if (j.last_stats) parts.push(JSON.stringify(j.last_stats, null, 2));
      el.textContent = parts.filter(Boolean).join(String.fromCharCode(10));
    } catch (e) { el.textContent = 'Ошибка опроса: ' + e; }
  }
  function showCfgMessage(ok, text) {
    const wrap = document.getElementById('cfgMessageWrap');
    const body = document.getElementById('cfgMessageText');
    wrap.classList.remove(
      'hidden',
      'bg-emerald-900/30', 'border-emerald-800', 'text-emerald-400',
      'bg-rose-900/30', 'border-rose-800', 'text-rose-400'
    );
    if (ok) {
      wrap.classList.add('bg-emerald-900/30', 'border', 'border-emerald-800', 'text-emerald-400');
    } else {
      wrap.classList.add('bg-rose-900/30', 'border', 'border-rose-800', 'text-rose-400');
    }
    body.textContent = text || '';
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
    pollSync();
  };
  setInterval(pollSync, 1200);
  pollSync();
  window.addEventListener('pageshow', function () {
    pollSync();
  });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') pollSync();
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

    from egisz_monitor_corp.sync_routes import register_sync_routes

    register_sync_routes(app, config_path)

    return app


def run_dev() -> None:
    import os

    app = create_app()
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "8765"))
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
