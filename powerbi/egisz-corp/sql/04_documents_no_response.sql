-- Dashboard 04: Документы без ответа (Corp) — metabase_dashboards/04_documents_no_response.json

SELECT *
FROM public.v_rpt_documents_no_response_ui
ORDER BY "Отправлено" DESC NULLS LAST;
