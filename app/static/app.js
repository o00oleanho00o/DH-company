/* global window, document, fetch, URL, FormData, Blob */

(() => {
  "use strict";

  const API_BASE = (window.__API_BASE__ || "").replace(/\/$/, "");
  const FILE_SIZE_LIMIT = 50 * 1024 * 1024;
  const SOURCE_TYPES = [
    ["supplier_price_list", "Bảng giá nhà cung cấp"],
    ["labor", "Bảng giá nhân công"],
    ["historical_boq", "Báo giá / BOQ lịch sử"],
    ["new_boq", "BOQ mới"],
    ["panel_bom", "BOM tủ điện"],
    ["mixed", "Workbook hỗn hợp"],
    ["unknown", "Chưa xác định"],
  ];
  const SOURCE_TYPE_LABELS = {
    SUPPLIER_PRICE: "Bảng giá nhà cung cấp",
    SUPPLIER_PRICE_LIST: "Bảng giá nhà cung cấp",
    LABOR: "Bảng giá nhân công",
    HISTORICAL_BOQ: "Báo giá / BOQ lịch sử",
    BOQ: "Báo giá / BOQ lịch sử",
    NEW_BOQ: "BOQ mới",
    PANEL_BOM: "BOM tủ điện",
    MIXED: "Workbook hỗn hợp",
    UNKNOWN: "Chưa xác định",
  };
  const navLabels = {
    dashboard: "Tổng quan",
    import: "Kho dữ liệu",
    quotations: "Báo giá",
    catalog: "Danh mục & giá",
    benchmark: "Đánh giá độ phủ",
    new: "Tạo báo giá",
    quotation: "Chi tiết báo giá",
    review: "Bàn rà soát",
  };

  const state = {
    route: "dashboard",
    sources: [],
    quotations: [],
    catalogStats: null,
    catalogItems: [],
    catalogTotal: 0,
    catalogPage: 0,
    catalogPageSize: 100,
    catalogFacets: { categories: [], sources: [], statuses: [] },
    catalogLoading: false,
    catalogFilters: {
      query: "",
      kind: "all",
      category: "all",
      sourceId: "all",
      status: "all",
    },
    activeSource: null,
    sourceDetailLoading: false,
    sourceDetailError: "",
    activeCatalogItem: null,
    catalogItemLoading: false,
    benchmark: null,
    activeQuotation: null,
    reviewItems: [],
    selectedReviewId: null,
    reviewFilter: "review",
    reviewQuery: "",
    dashboardLoading: false,
    sourceLoading: false,
    quotationLoading: false,
    benchmarkLoading: false,
    currentImportFiles: [],
    currentImportResults: [],
    newQuotationFiles: [],
    importBusy: false,
    importPreviewBusy: false,
    quotationBusy: false,
    lastError: "",
    aiStatus: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const pageView = $("#page-view");
  const modalRoot = $("#modal-root");
  const toastRegion = $("#toast-region");

  function icon(name, className = "") {
    return `<svg class="${className}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // API responses can contain a report URL originating from persisted
  // benchmark metadata. Escape HTML is not enough for href attributes because
  // schemes such as javascript: remain executable after entity decoding.
  // Keep only ordinary HTTP(S) or same-origin relative URLs.
  function safeHref(value) {
    const raw = String(value ?? "").trim();
    if (!raw || raw.startsWith("//")) return "";
    try {
      const parsed = new URL(raw, window.location.origin);
      if (!["http:", "https:"].includes(parsed.protocol)) return "";
      return escapeHtml(raw);
    } catch (_) {
      return "";
    }
  }

  function formatNumber(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    const number = Number(value);
    if (Number.isNaN(number)) return escapeHtml(value);
    return new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 }).format(number);
  }

  function formatCurrency(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    const number = Number(value);
    if (Number.isNaN(number)) return escapeHtml(value);
    return `${new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 0 }).format(number)} ₫`;
  }

  function formatDate(value, fallback = "Chưa có") {
    if (!value) return fallback;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return escapeHtml(value);
    return date.toLocaleDateString("vi-VN", { day: "2-digit", month: "2-digit", year: "numeric" });
  }

  function formatDateTime(value, fallback = "Chưa có") {
    if (!value) return fallback;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return escapeHtml(value);
    return date.toLocaleString("vi-VN", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function fileSize(size) {
    if (!Number.isFinite(Number(size))) return "";
    const units = ["B", "KB", "MB", "GB"];
    let value = Number(size);
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function percentage(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    const numeric = Number(value);
    if (Number.isNaN(numeric)) return escapeHtml(value);
    const normalized = numeric <= 1 ? numeric * 100 : numeric;
    return `${Math.round(normalized * 10) / 10}%`;
  }

  function rawArray(payload, keys = []) {
    if (Array.isArray(payload)) return payload;
    for (const key of keys) {
      if (Array.isArray(payload?.[key])) return payload[key];
    }
    return [];
  }

  function pick(obj, keys, fallback = "") {
    for (const key of keys) {
      if (obj && obj[key] !== undefined && obj[key] !== null && obj[key] !== "") return obj[key];
    }
    return fallback;
  }

  function countValue(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function normalizeSource(source = {}) {
    const lifecycle = source.lifecycle || source.lifecycle_state || {};
    const summary = source.summary || source.counts || source.catalog || {};
    const references = source.reference_counts || source.references || source.impact?.references || {};
    const derivedReferenceCount = pick(
      references,
      ["derived_references", "total", "count"],
      Object.values(references || {}).reduce((total, value) => total + (Number.isFinite(Number(value)) ? Number(value) : 0), 0),
    );
    return {
      id: pick(source, ["id", "source_id", "workbook_id"], ""),
      filename: pick(source, ["filename", "file_name", "name"], "Tệp không tên"),
      detectedType: pick(source, ["detected_type", "document_type", "type"], "unknown"),
      confirmedType: pick(source, ["confirmed_type", "classification"], ""),
      // Lifecycle status controls whether a source participates in new
      // pricing; processing status is retained separately for diagnostics.
      // Prefer lifecycle here so an archived/superseded workbook is visibly
      // different from an active one in every table.
      status: pick(
        source,
        ["lifecycle_status", "source_status", "record_status"],
        pick(lifecycle, ["status", "state"], pick(source, ["status", "processing_status"], "ready")),
      ),
      processingStatus: pick(source, ["processing_status", "processingState"], "ready"),
      lifecycleStatus: pick(
        source,
        ["lifecycle_status", "source_status", "record_status", "status"],
        pick(lifecycle, ["status", "state"], ""),
      ),
      lifecycleReason: pick(
        source,
        ["lifecycle_reason", "status_reason", "archive_reason"],
        pick(lifecycle, ["reason", "status_reason"], ""),
      ),
      archivedAt: pick(source, ["archived_at", "outdated_at"], pick(lifecycle, ["archived_at"], "")),
      supersededBy: pick(
        source,
        ["superseded_by", "superseded_by_source_id", "superseded_by_source_file_id"],
        pick(lifecycle, ["superseded_by", "superseded_by_source_file_id"], ""),
      ),
      referencedCount: pick(source, ["referenced_count", "reference_count", "used_by_count"], derivedReferenceCount),
      sheets: pick(
        source,
        ["sheet_count", "sheets_count", "number_of_sheets"],
        pick(summary, ["sheets", "sheet_count"], source.sheets?.length || 0),
      ),
      rows: pick(
        source,
        ["extracted_rows", "data_rows", "row_count", "rows"],
        pick(summary, ["data_rows", "extracted_rows", "rows", "row_count"], 0),
      ),
      warnings: pick(source, ["warning_count", "warnings"], 0),
      uploadedAt: pick(source, ["uploaded_at", "created_at", "imported_at"], ""),
      summary: {
        sheets: pick(summary, ["sheets", "sheet_count"], source.sheets?.length || 0),
        rows: pick(summary, ["rows", "row_count"], 0),
        dataRows: pick(summary, ["data_rows", "extracted_rows"], 0),
        products: pick(summary, ["products", "product_count", "materials", "material_count"], 0),
        prices: pick(summary, ["prices", "price_count", "product_prices", "observations"], 0),
        priceObservations: pick(summary, ["price_observations", "observation_count"], 0),
        labor: pick(summary, ["labor", "labor_count", "labor_items", "labors"], 0),
        laborRates: pick(summary, ["labor_rates", "rate_count"], 0),
        boq: pick(summary, ["boq", "boq_count", "boq_items"], 0),
        catalogLinks: pick(summary, ["catalog_links", "link_count"], 0),
        warnings: pick(summary, ["warnings", "warning_count"], pick(source, ["warning_count"], 0)),
      },
      sheetsData: rawArray(source, ["sheets", "worksheets"]),
      rawRowsLoaded:
        Boolean(source.raw_rows_loaded || source.include_rows) ||
        rawArray(source, ["sheets", "worksheets"]).some((sheet) => rawArray(sheet, ["rows"]).length > 0),
      products: rawArray(source, ["products", "product_records", "catalog_products"]),
      prices: rawArray(source, ["prices", "price_records", "price_observations", "observations"]),
      labor: rawArray(source, ["labor", "labor_items", "labor_records", "labor_rates"]),
      boq: rawArray(source, ["boq", "boq_items", "boq_records"]),
      metadata: source.metadata || {},
      raw: source,
    };
  }

  function normalizeCatalogItem(item = {}, kindHint = "") {
    const sourceValue =
      [item.source, item.provenance, item.price_source].find(
        (candidate) => candidate && (!Array.isArray(candidate) || candidate.length > 0),
      ) || {};
    const source = Array.isArray(sourceValue) ? sourceValue[0] || {} : sourceValue;
    const kind = String(
      pick(item, ["kind", "record_type", "item_type", "type"], kindHint || (item.labor_item_id || item.labor_id ? "labor" : "product")),
    ).toLowerCase();
    const isLabor = ["labor", "labour", "labor_item", "labor_rate", "nhan_cong"].includes(kind);
    const observations = rawArray(item, ["observations", "prices", "price_observations", "history"]);
    const current = item.current_price || item.current || item.latest_price || item.latest_rate || {};
    const scalar = (value, keys) => {
      const candidate = pick(value, keys, null);
      return candidate && typeof candidate === "object" ? null : candidate;
    };
    const currentValue = isLabor
      ? scalar(item, ["current_rate", "labor_rate", "rate_value", "price"]) ?? scalar(current, ["rate", "price", "value"])
      : scalar(item, ["current_price", "net_price", "unit_price", "price"]) ?? scalar(current, ["net_price", "price", "value"]);
    return {
      id: pick(item, ["id", "item_id", isLabor ? "labor_item_id" : "product_id"], ""),
      kind: isLabor ? "labor" : kind === "price" || kind === "observation" ? "price" : "product",
      name: pick(item, ["name", "normalized_name", "product_name", "description", "canonical_name"], "Hạng mục chưa đặt tên"),
      code: pick(item, ["code", "product_code", "item_code", "labor_code"], ""),
      category: pick(item, ["category", "group", "section", "subcategory"], "Chưa phân loại"),
      subcategory: pick(item, ["subcategory", "sub_category"], ""),
      brand: pick(item, ["brand", "manufacturer"], ""),
      origin: pick(item, ["origin", "country"], ""),
      unit: pick(item, ["unit", "uom"], ""),
      status: pick(item, ["status", "lifecycle_status", "record_status"], "active"),
      currentPrice: currentValue,
      currency: pick(item, ["currency"], pick(current, ["currency"], "VND")),
      // Entity created_at is not a price effective date. Showing it as such
      // made unpriced catalog rows look current even when no rate/source exists.
      effectiveDate: pick(item, ["effective_date", "price_date", "valid_from"], pick(current, ["effective_date", "date"], "")),
      validTo: pick(item, ["valid_to", "expires_at"], pick(current, ["valid_to"], "")),
      supplier: pick(item, ["supplier", "vendor"], pick(current, ["supplier"], "")),
      taxMode: pick(item, ["tax_mode", "tax_basis"], pick(current, ["tax_mode"], "ex_vat")),
      confidence: pick(item, ["confidence", "score"], pick(current, ["confidence"], null)),
      observationCount: pick(item, ["observation_count", "price_count", "history_count"], observations.length),
      sourceId: pick(item, ["source_file_id", "source_id", "workbook_id"], pick(source, ["source_file_id", "source_id", "file_id", "id"], "")),
      sourceFilename: pick(item, ["source_filename", "filename", "workbook"], pick(source, ["filename", "name"], "")),
      sourceSheet: pick(item, ["source_sheet", "sheet_name"], pick(source, ["sheet_name", "sheet"], "")),
      sourceRow: pick(item, ["source_row", "row_no", "row_number"], pick(source, ["row_no", "row"], "")),
      source,
      observations,
      aliases: rawArray(item, ["aliases", "alias"]),
      technicalAttributes: item.technical_attributes || item.technicalAttributes || {},
      raw: item,
    };
  }

  function normalizeQuotation(quotation = {}) {
    const stats = quotation.stats || quotation.summary || {};
    return {
      id: pick(quotation, ["id", "quotation_id", "project_id"], ""),
      projectName: pick(quotation, ["project_name", "name", "title"], "Báo giá chưa đặt tên"),
      customer: pick(quotation, ["customer", "client"], "Chưa cập nhật"),
      quotationDate: pick(quotation, ["quotation_date", "date", "created_at"], ""),
      status: pick(quotation, ["status", "processing_status"], "draft"),
      sourceFilename: pick(quotation, ["source_filename", "filename", "boq_filename"], ""),
      updatedAt: pick(quotation, ["updated_at", "created_at"], ""),
      totalRows: pick(quotation, ["total_rows", "row_count"], pick(stats, ["total_rows", "total"], 0)),
      materialMatched: pick(
        quotation,
        ["material_matched", "materialMatched"],
        pick(stats, ["material_matched", "materialMatched"], 0),
      ),
      laborMatched: pick(
        quotation,
        ["labor_matched", "laborMatched"],
        pick(stats, ["labor_matched", "laborMatched"], 0),
      ),
      reviewCount: pick(
        quotation,
        ["review_required", "review_count", "need_review"],
        pick(stats, ["review_required", "review_count", "need_review"], 0),
      ),
      noMatch:
        countValue(
          pick(quotation, ["no_match", "no_match_count"], pick(stats, ["no_match", "no_match_count"], 0)),
        ) +
        countValue(
          pick(
            quotation,
            ["no_price_found", "no_price_count"],
            pick(stats, ["no_price_found", "no_price_count"], 0),
          ),
        ),
      coverage: pick(quotation, ["coverage", "auto_coverage"], pick(stats, ["coverage", "auto_coverage"], null)),
      totalValue: pick(quotation, ["total_value", "grand_total"], pick(stats, ["total_value", "grand_total"], null)),
      raw: quotation,
    };
  }

  function normalizeReviewItem(item = {}) {
    const recommendation = item.recommended_match || item.recommendation || item.match || {};
    const materialSource = item.material_source || item.material_provenance || {};
    const laborSource = item.labor_source || item.labor_provenance || {};
    const alternatives = item.alternatives || {};
    const listedCandidates = rawArray(item, ["candidates", "top_candidates"]);
    const candidates = listedCandidates.length
      ? listedCandidates
      : [
          ...rawArray(alternatives, ["material"]).map((candidate) => ({ ...candidate, candidate_type: "material" })),
          ...rawArray(alternatives, ["labor"]).map((candidate) => ({ ...candidate, candidate_type: "labor" })),
        ];
    const fallbackRecommendation =
      Object.keys(recommendation).length > 0
        ? recommendation
        : item.matched_product
          ? { ...item.matched_product, candidate_type: "material", score: item.material_confidence }
          : item.matched_labor
            ? { ...item.matched_labor, candidate_type: "labor", score: item.labor_confidence }
            : {};
    return {
      id: pick(item, ["id", "boq_item_id", "item_id"], ""),
      rawDescription: pick(item, ["raw_description", "description", "name"], "Dòng BOQ chưa có mô tả"),
      normalizedDescription: pick(item, ["normalized_description", "normalized_name"], ""),
      section: pick(item, ["section", "group", "category"], ""),
      code: pick(item, ["product_code", "code", "item_code"], ""),
      unit: pick(item, ["unit", "uom"], ""),
      quantity: pick(item, ["quantity", "qty"], ""),
      brand: pick(item, ["brand", "manufacturer"], ""),
      origin: pick(item, ["origin", "country"], ""),
      materialPrice: pick(item, ["material_price", "materialPrice", "unit_material_price"], null),
      laborPrice: pick(item, ["labor_price", "laborPrice", "unit_labor_price"], null),
      materialConfidence: pick(item, ["material_confidence", "materialConfidence"], null),
      laborConfidence: pick(item, ["labor_confidence", "laborConfidence"], null),
      status: pick(item, ["status", "review_status"], "review_required"),
      explanation: pick(item, ["explanation", "match_explanation", "warning"], ""),
      recommendation: fallbackRecommendation,
      candidates,
      materialSource,
      laborSource,
      raw: item,
    };
  }

  function sourceTypeLabel(type) {
    const raw = String(type || "");
    const found = SOURCE_TYPES.find(([value]) => value === raw.toLowerCase());
    if (SOURCE_TYPE_LABELS[raw.toUpperCase()]) return SOURCE_TYPE_LABELS[raw.toUpperCase()];
    return found ? found[1] : type ? String(type).replace(/_/g, " ") : "Chưa xác định";
  }

  function sourceStatus(status) {
    const value = String(status || "").toLowerCase();
    if (["active", "enabled", "current"].includes(value)) {
      return { label: "Đang sử dụng", className: "badge-success" };
    }
    if (["archived", "archive", "inactive", "retired"].includes(value)) {
      return { label: "Đã lưu trữ", className: "badge-neutral" };
    }
    if (["superseded", "outdated", "replaced"].includes(value)) {
      return { label: "Đã thay thế", className: "badge-warning" };
    }
    if (["completed", "complete", "ready", "imported", "success", "done"].includes(value)) {
      return { label: "Sẵn sàng", className: "badge-success" };
    }
    if (["processing", "queued", "running", "pending"].includes(value)) {
      return { label: "Đang xử lý", className: "badge-blue" };
    }
    if (["failed", "error"].includes(value)) {
      return { label: "Lỗi", className: "badge-danger" };
    }
    return { label: "Nháp", className: "badge-neutral" };
  }

  function quotationStatus(status) {
    const value = String(status || "").toLowerCase();
    if (["completed", "complete", "priced", "done", "exported"].includes(value)) {
      return { label: "Đã tính giá", className: "badge-success" };
    }
    if (["running", "processing", "queued"].includes(value)) {
      return { label: "Đang chạy", className: "badge-blue" };
    }
    if (["review", "review_required", "needs_review", "price_drift_warning", "external_quotation_required", "needs_supplier_quotation"].includes(value)) {
      return { label: "Cần rà soát", className: "badge-warning" };
    }
    if (["failed", "error"].includes(value)) {
      return { label: "Lỗi", className: "badge-danger" };
    }
    return { label: "Bản nháp", className: "badge-neutral" };
  }

  function reviewStatus(status) {
    const value = String(status || "").toLowerCase();
    if (["approved", "auto_approved", "accepted", "matched"].includes(value)) {
      return { label: "Đã duyệt", className: "badge-success" };
    }
    if (["external_quotation_required", "needs_supplier_quotation"].includes(value)) {
      return { label: "Cần báo giá NCC", className: "badge-danger" };
    }
    if (["no_match", "no_price_found", "unmatched"].includes(value)) {
      return { label: "Chưa có giá", className: "badge-danger" };
    }
    if (["ignored", "skipped"].includes(value)) {
      return { label: "Bỏ qua", className: "badge-neutral" };
    }
    return { label: "Cần rà soát", className: "badge-warning" };
  }

  function candidateKind(candidate = {}) {
    const value = String(candidate.candidate_type || candidate.type || candidate.kind || "material").toLowerCase();
    return ["labor", "labour", "labor_item", "labor_rate", "nhan_cong"].includes(value) ? "labor" : "material";
  }

  function setHealth(status, label) {
    const element = $("#api-health");
    if (!element) return;
    element.classList.remove("is-online", "is-error");
    if (status === "online") element.classList.add("is-online");
    if (status === "error") element.classList.add("is-error");
    const labelElement = $(".api-health-label", element);
    if (labelElement) labelElement.textContent = label;
  }

  function setAIHealth(status, label, details = "") {
    const element = $("#ai-health");
    if (!element) return;
    element.classList.remove("is-online", "is-warning", "is-error");
    if (status === "online") element.classList.add("is-online");
    if (status === "warning") element.classList.add("is-warning");
    if (status === "error") element.classList.add("is-error");
    const labelElement = $(".ai-health-label", element);
    if (labelElement) labelElement.textContent = label;
    if (details) element.title = details;
  }

  function toast(message, type = "info", title = "") {
    if (!toastRegion) return;
    const item = document.createElement("div");
    item.className = `toast is-${type}`;
    const iconName = type === "success" ? "check-circle" : type === "error" ? "alert" : type === "warning" ? "alert" : "info";
    item.innerHTML = `
      <span class="toast-icon">${icon(iconName)}</span>
      <div class="toast-copy">
        ${title ? `<div class="toast-title">${escapeHtml(title)}</div>` : ""}
        <div>${escapeHtml(message)}</div>
      </div>
      <button class="icon-button toast-close" aria-label="Đóng thông báo">${icon("x")}</button>
    `;
    toastRegion.appendChild(item);
    item.querySelector(".toast-close")?.addEventListener("click", () => item.remove());
    window.setTimeout(() => item.remove(), 5600);
  }

  function setBreadcrumb(route) {
    const breadcrumb = $("#breadcrumb");
    if (!breadcrumb) return;
    const label = navLabels[route] || navLabels[route.split("/")[0]] || "Tổng quan";
    breadcrumb.innerHTML = `<span>Không gian làm việc</span>${icon("chevron-right")}<strong>${escapeHtml(label)}</strong>`;
  }

  async function request(path, options = {}) {
    const url = `${API_BASE}${path}`;
    const headers = new Headers(options.headers || {});
    if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    let response;
    try {
      response = await fetch(url, { ...options, headers });
    } catch (error) {
      setHealth("error", "Không kết nối được API");
      throw new Error("Không thể kết nối tới máy chủ. Kiểm tra FastAPI đang chạy.");
    }
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      payload = await response.json().catch(() => null);
    } else if (contentType.includes("text/")) {
      payload = await response.text().catch(() => "");
    } else if (response.ok) {
      payload = await response.blob().catch(() => null);
    }
    if (!response.ok) {
      const detailValue = payload?.detail || payload?.message || payload?.error || `HTTP ${response.status}`;
      const detail =
        detailValue && typeof detailValue === "object"
          ? detailValue.message || detailValue.detail || detailValue.error || JSON.stringify(detailValue)
          : detailValue;
      const error = new Error(String(detail));
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    setHealth("online", "API đang hoạt động");
    return payload;
  }

  const api = {
    async health() {
      return request("/api/health");
    },
    async sources() {
      return rawArray(await request("/api/sources"), ["sources", "items", "data"]);
    },
    async catalogStats() {
      return request("/api/catalog/stats");
    },
    async source(id, options = {}) {
      const params = new URLSearchParams();
      params.set("include_rows", String(Boolean(options.includeRows)));
      params.set("limit", String(options.limit || 500));
      return request(`/api/sources/${encodeURIComponent(id)}?${params.toString()}`);
    },
    async catalogItems(filters = {}) {
      const params = new URLSearchParams();
      const mapping = {
        kind: filters.kind,
        category: filters.category,
        source_id: filters.sourceId,
        status: filters.status === "all" ? "ALL" : filters.status,
        q: filters.query,
        search: filters.query,
        limit: filters.limit || 200,
        offset: filters.offset || 0,
      };
      Object.entries(mapping).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "" && value !== "all") params.set(key, value);
      });
      try {
        const payload = await request(`/api/catalog/items${params.toString() ? `?${params.toString()}` : ""}`);
        if (payload && typeof payload === "object" && !Array.isArray(payload)) payload.__serverFiltered = true;
        return payload;
      } catch (error) {
        // Older servers only expose separate product/labor endpoints. Keep the
        // new screen usable while the API is being upgraded.
        if (error.status !== 404) throw error;
        const kinds = filters.kind === "labor" ? ["labor"] : filters.kind === "product" ? ["product"] : ["product", "labor"];
        const responses = await Promise.all(
          kinds.map(async (kind) => {
            const fallbackParams = new URLSearchParams();
            if (filters.query) fallbackParams.set("q", filters.query);
            if (filters.category && filters.category !== "all") fallbackParams.set("category", filters.category);
            if (filters.sourceId && filters.sourceId !== "all") fallbackParams.set("source_id", filters.sourceId);
            const path = kind === "labor" ? "/api/catalog/labor" : "/api/catalog/products";
            try {
              return rawArray(await request(`${path}${fallbackParams.toString() ? `?${fallbackParams.toString()}` : ""}`), ["items", "data", "products", "labor"]);
            } catch (fallbackError) {
              if (fallbackError.status === 404) return [];
              throw fallbackError;
            }
          }),
        );
        const items = responses.flat();
        return { items, total: items.length, categories: [], sources: [], __serverFiltered: false };
      }
    },
    async catalogItem(kind, id) {
      const normalizedKind = kind === "labor" ? "labor" : "product";
      try {
        return await request(`/api/catalog/items/${encodeURIComponent(normalizedKind)}/${encodeURIComponent(id)}`);
      } catch (error) {
        if (error.status !== 404) throw error;
        const path = normalizedKind === "labor" ? "/api/catalog/labor" : "/api/catalog/products";
        return request(`${path}/${encodeURIComponent(id)}`);
      }
    },
    async archiveSource(id) {
      return request(`/api/sources/${encodeURIComponent(id)}/archive?reason=${encodeURIComponent("archived_by_user")}`, {
        method: "POST",
      });
    },
    async restoreSource(id) {
      return request(`/api/sources/${encodeURIComponent(id)}/restore`, { method: "POST", body: JSON.stringify({}) });
    },
    async deleteSource(id) {
      return request(`/api/sources/${encodeURIComponent(id)}`, { method: "DELETE" });
    },
    async archiveCatalogItem(item) {
      const kind = item.kind === "labor" ? "labor" : "product";
      try {
        return await request(`/api/catalog/items/${encodeURIComponent(kind)}/${encodeURIComponent(item.id)}/archive?reason=${encodeURIComponent("archived_by_user")}`, {
          method: "POST",
        });
      } catch (error) {
        if (error.status !== 404) throw error;
        return request(`/api/catalog/${encodeURIComponent(kind)}/${encodeURIComponent(item.id)}/archive`, { method: "POST" });
      }
    },
    async restoreCatalogItem(item) {
      const kind = item.kind === "labor" ? "labor" : "product";
      return request(`/api/catalog/items/${encodeURIComponent(kind)}/${encodeURIComponent(item.id)}/restore`, { method: "POST" });
    },
    async deleteCatalogItem(item) {
      const kind = item.kind === "labor" ? "labor" : "product";
      try {
        return await request(`/api/catalog/items/${encodeURIComponent(kind)}/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      } catch (error) {
        if (error.status !== 404) throw error;
        return request(`/api/catalog/${encodeURIComponent(kind)}/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      }
    },
    async quotations() {
      return rawArray(await request("/api/quotations"), ["quotations", "items", "data"]);
    },
    async quotation(id) {
      return request(`/api/quotations/${encodeURIComponent(id)}`);
    },
    async reviewItems(id) {
      const response = await request(`/api/quotations/${encodeURIComponent(id)}/review`);
      return rawArray(response, ["items", "boq_items", "review_items", "data"]);
    },
    async importFiles(files, classifications = {}) {
      const form = new FormData();
      files.forEach((file) => form.append("files", file, file.name));
      form.append("confirm_classification", JSON.stringify(classifications));
      return request("/api/import", { method: "POST", body: form });
    },
    async importPreview(file) {
      const form = new FormData();
      form.append("file", file, file.name);
      return request("/api/import/preview", { method: "POST", body: form });
    },
    async createQuotation(fields, files) {
      if (files?.length) {
        const form = new FormData();
        files.forEach((file) => form.append("files", file, file.name));
        Object.entries(fields).forEach(([key, value]) => {
          if (value !== undefined && value !== null) form.append(key, value);
        });
        return request("/api/quotations", { method: "POST", body: form });
      }
      return request("/api/quotations", { method: "POST", body: JSON.stringify(fields) });
    },
    async runQuotation(id) {
      return request(`/api/quotations/${encodeURIComponent(id)}/run`, { method: "POST", body: JSON.stringify({}) });
    },
    async reviewBOQItem(id, payload) {
      return request(`/api/boq-items/${encodeURIComponent(id)}/review`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    async exportQuotation(id) {
      const response = await fetch(`${API_BASE}/api/quotations/${encodeURIComponent(id)}/export`);
      if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(text || `Không thể xuất file (HTTP ${response.status})`);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") || "";
      const match = disposition.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
      return { blob, filename: match ? decodeURIComponent(match[1]) : `bao-gia-${id}.xlsx` };
    },
    async benchmark() {
      return request("/api/benchmark");
    },
  };

  function describeAIStatus(payload) {
    const ai = payload?.ai || {};
    const calls = Number(ai.calls_made);
    const maxCalls = Number(ai.max_calls);
    const usage =
      Number.isFinite(calls) && Number.isFinite(maxCalls) && maxCalls > 0
        ? ` • ${calls}/${maxCalls} lượt trong batch`
        : "";
    if (ai.enabled && ai.configured) {
      return {
        status: "online",
        label: `AI semantic: bật${usage}`,
        details: `LLM chỉ rerank candidate mơ hồ; không tạo giá hoặc provenance.${usage}`,
      };
    }
    if (ai.enabled && !ai.configured) {
      return {
        status: "warning",
        label: "AI semantic: thiếu cấu hình",
        details: "LLM được yêu cầu nhưng chưa đủ base URL, model hoặc API key; engine giữ deterministic.",
      };
    }
    return {
      status: "warning",
      label: "AI semantic: tắt • deterministic",
      details: "Pricing engine đang chạy deterministic; AI chỉ là lớp semantic tùy chọn.",
    };
  }

  async function loadAIHealth() {
    try {
      const payload = await api.health();
      state.aiStatus = payload?.ai || {};
      const description = describeAIStatus(payload);
      setAIHealth(description.status, description.label, description.details);
    } catch (error) {
      state.aiStatus = null;
      setAIHealth("error", "AI semantic: chưa xác định", "Không đọc được trạng thái AI từ API.");
    }
  }

  function routeFromHash() {
    const hash = window.location.hash.replace(/^#\/?/, "") || "dashboard";
    const [base, id] = hash.split("/");
    if (["dashboard", "import", "new", "quotations", "catalog", "benchmark"].includes(base)) {
      return { base, id: null };
    }
    if (base === "quotation" && id) return { base, id };
    if (base === "review" && id) return { base, id };
    return { base: "dashboard", id: null };
  }

  function navigate(route) {
    const target = route.startsWith("#") ? route : `#${route}`;
    if (window.location.hash !== target) {
      window.location.hash = target;
    } else {
      renderRoute();
    }
    closeSidebar();
  }

  function openSidebar() {
    $("#sidebar")?.classList.add("is-open");
    $("#sidebar-backdrop")?.classList.add("is-visible");
  }

  function closeSidebar() {
    $("#sidebar")?.classList.remove("is-open");
    $("#sidebar-backdrop")?.classList.remove("is-visible");
  }

  function skeletonCard() {
    return `<div class="loading-skeleton" aria-label="Đang tải"></div>`;
  }

  function emptyState({ iconName = "file", title, description, actionLabel, action }) {
    return `
      <div class="empty-state">
        <div class="empty-state-icon">${icon(iconName)}</div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(description)}</p>
        ${actionLabel && action ? `<button class="button button-secondary" data-action="${escapeHtml(action)}">${icon("arrow-right")}<span>${escapeHtml(actionLabel)}</span></button>` : ""}
      </div>
    `;
  }

  function pageIntro(eyebrow, title, description, actions = "") {
    return `
      <div class="page-intro">
        <div>
          <p class="eyebrow">${escapeHtml(eyebrow)}</p>
          <h1>${escapeHtml(title)}</h1>
          <p>${escapeHtml(description)}</p>
        </div>
        ${actions ? `<div class="intro-actions">${actions}</div>` : ""}
      </div>
    `;
  }

  function statCard(label, value, meta, iconName, tone = "") {
    return `
      <article class="card stat-card">
        <div class="stat-top">
          <span class="stat-label">${escapeHtml(label)}</span>
          <span class="stat-icon ${tone ? `is-${tone}` : ""}">${icon(iconName)}</span>
        </div>
        <div class="stat-value">${value}</div>
        <div class="stat-meta">${escapeHtml(meta)}</div>
      </article>
    `;
  }

  function getCatalogNumber(keys, fallback = 0) {
    return pick(state.catalogStats || {}, keys, fallback);
  }

  async function loadDashboard() {
    state.dashboardLoading = true;
    renderDashboard();
    const results = await Promise.allSettled([api.sources(), api.quotations(), api.catalogStats()]);
    const [sources, quotations, stats] = results;
    if (sources.status === "fulfilled") state.sources = sources.value.map(normalizeSource);
    if (quotations.status === "fulfilled") state.quotations = quotations.value.map(normalizeQuotation);
    if (stats.status === "fulfilled") state.catalogStats = stats.value || {};
    state.dashboardLoading = false;
    renderDashboard();
    updateNavCount();
  }

  function updateNavCount() {
    const count = state.sources.filter((source) => source.status && !["ready", "completed", "complete", "imported", "success", "done"].includes(String(source.status).toLowerCase())).length;
    const badge = $("#nav-source-count");
    if (badge) badge.textContent = count ? String(count) : "";
  }

  function renderDashboard() {
    if (state.route !== "dashboard") return;
    const quotationCount = state.quotations.length;
    const sourceCount = state.sources.length;
    const recent = [...state.quotations]
      .sort((a, b) => new Date(b.updatedAt || b.quotationDate || 0) - new Date(a.updatedAt || a.quotationDate || 0))
      .slice(0, 5);
    const reviewTotal = state.quotations.reduce((sum, item) => sum + Number(item.reviewCount || 0), 0);
    const productCount = getCatalogNumber(["products", "product_count", "materials", "material_count"], 0);
    const laborCount = getCatalogNumber(["labor_items", "labor_count", "labors"], 0);
    pageView.innerHTML = `
      ${pageIntro(
        "DH PRICING HUB",
        "Chào mừng trở lại",
        "Từ dữ liệu Excel rời rạc đến một báo giá có nguồn, có độ tin cậy và sẵn sàng để kỹ sư rà soát.",
        `<button class="button button-primary button-large" data-action="new-quotation">${icon("file-plus")}<span>Tạo báo giá mới</span></button>`,
      )}
      <section class="stats-grid" aria-label="Chỉ số nhanh">
        ${statCard("Báo giá", formatNumber(quotationCount), "Tổng số hồ sơ trong hệ thống", "file", "navy")}
        ${statCard("Nguồn dữ liệu", formatNumber(sourceCount), "Workbook đã nhập và lưu provenance", "upload", "blue")}
        ${statCard("Mục cần rà soát", formatNumber(reviewTotal), "Các dòng chưa được tự động duyệt", "alert", "amber")}
        ${statCard("Sản phẩm trong danh mục", formatNumber(productCount), `${formatNumber(laborCount)} mục nhân công`, "database", "")}
      </section>
      <div class="dashboard-grid">
        <section class="card list-card">
          <div class="card-header">
            <div class="card-header-copy">
              <h2>Báo giá gần đây</h2>
              <p>Theo dõi tiến độ các hồ sơ mới nhất.</p>
            </div>
            <button class="button button-quiet button-small" data-action="view-quotations">Xem tất cả ${icon("arrow-right")}</button>
          </div>
          ${
            state.dashboardLoading
              ? `<div class="card-body">${skeletonCard()}</div>`
              : recent.length
                ? recent.map((item) => renderQuotationListRow(item, true)).join("")
                : emptyState({
                    iconName: "file-plus",
                    title: "Chưa có báo giá nào",
                    description: "Bắt đầu bằng một BOQ mới để hệ thống tìm vật tư, nhân công và nguồn giá phù hợp.",
                    actionLabel: "Tạo báo giá đầu tiên",
                    action: "new-quotation",
                  })
          }
        </section>
        <section class="card">
          <div class="card-header">
            <div class="card-header-copy">
              <h2>Lối tắt</h2>
              <p>Các tác vụ thường dùng của kỹ sư báo giá.</p>
            </div>
          </div>
          <div class="quick-actions">
            <button class="quick-action" data-action="import">
              <span class="quick-action-icon">${icon("upload")}</span>
              <span><strong>Nhập dữ liệu</strong><span>Thêm bảng giá, nhân công hoặc BOQ lịch sử.</span></span>
            </button>
            <button class="quick-action" data-action="new-quotation">
              <span class="quick-action-icon">${icon("file-plus")}</span>
              <span><strong>Tạo báo giá</strong><span>Upload BOQ và chạy pipeline định giá.</span></span>
            </button>
            <button class="quick-action" data-action="catalog">
              <span class="quick-action-icon">${icon("database")}</span>
              <span><strong>Danh mục &amp; giá</strong><span>Kiểm tra dữ liệu đã chuẩn hóa.</span></span>
            </button>
            <button class="quick-action" data-action="benchmark">
              <span class="quick-action-icon">${icon("layers")}</span>
              <span><strong>Độ phủ tự động</strong><span>Đo coverage và các dòng rủi ro.</span></span>
            </button>
          </div>
        </section>
      </div>
    `;
  }

  function renderQuotationListRow(item, compact = false) {
    const status = quotationStatus(item.status);
    return `
      <button class="list-row" data-action="open-quotation" data-id="${escapeHtml(item.id)}">
        <span class="list-row-main">
          <span class="list-row-title">${escapeHtml(item.projectName)}</span>
          <span class="list-row-sub">${escapeHtml(item.customer)}${item.sourceFilename ? ` • ${escapeHtml(item.sourceFilename)}` : ""}</span>
        </span>
        <span class="badge ${status.className}">${status.label}</span>
        <span class="list-row-date">${formatDate(item.updatedAt || item.quotationDate)}</span>
      </button>
    `;
  }

  async function loadSources() {
    state.sourceLoading = true;
    renderImport();
    try {
      state.sources = (await api.sources()).map(normalizeSource);
    } catch (error) {
      state.lastError = error.message;
      toast(error.message, "error", "Không tải được kho dữ liệu");
    } finally {
      state.sourceLoading = false;
      renderImport();
      updateNavCount();
    }
  }

  function renderImport() {
    if (state.route !== "import") return;
    const queue = state.currentImportResults.length
      ? state.currentImportResults
      : state.currentImportFiles.map((file) => ({
          filename: file.name,
          detected_type: "unknown",
          status: "queued",
          sheet_count: 0,
          row_count: 0,
        }));
    pageView.innerHTML = `
      ${pageIntro(
        "KHO DỮ LIỆU",
        "Import Center",
        "Nạp một hoặc nhiều workbook. Hệ thống sẽ tự kiểm tra sheet, nhận diện loại tài liệu và lưu nguyên bản để audit.",
        `<button class="button button-secondary button-small" data-action="refresh-sources">${icon("refresh")}<span>Làm mới danh sách</span></button>`,
      )}
      <div class="import-grid">
        <section class="card">
          <div class="card-header">
            <div class="card-header-copy">
              <h2>Thêm workbook</h2>
              <p>Hỗ trợ .xlsx và .xls, tối đa 50 MB mỗi tệp.</p>
            </div>
          </div>
          <div class="card-body">
            <label class="file-dropzone ${state.importBusy ? "is-busy" : ""}" id="import-dropzone">
              <span class="file-dropzone-icon">${icon("upload")}</span>
              <strong>Kéo thả file vào đây</strong>
              <p>hoặc chọn từ máy tính. Có thể chọn nhiều file cùng lúc để xử lý theo lô.</p>
              <span class="button button-secondary button-small">Chọn file</span>
              <input id="import-file-input" type="file" accept=".xlsx,.xls" multiple ${state.importBusy ? "disabled" : ""} />
            </label>
            ${
              state.currentImportFiles.length
                ? `<div class="selected-files">${state.currentImportFiles.map(renderSelectedFile).join("")}</div>`
                : ""
            }
            ${
              state.currentImportFiles.length
                ? `<div class="callout callout-warning" style="margin-top:12px">${icon("info")}<span>Kiểm tra phân loại tự động ở cột bên phải trước khi xác nhận import. Giá chỉ được áp dụng khi có provenance.</span></div>`
                : ""
            }
            <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">
              ${
                state.currentImportFiles.length
                  ? `<button class="button button-secondary button-small" data-action="clear-import-files" ${state.importBusy ? "disabled" : ""}>Xóa lựa chọn</button>
                     <button class="button button-primary button-small" data-action="submit-import" ${state.importBusy ? "disabled" : ""}>${state.importBusy ? "Đang xử lý…" : "Xác nhận import"} ${icon("arrow-right")}</button>`
                  : ""
              }
            </div>
          </div>
        </section>
        <section class="card">
          <div class="card-header">
            <div class="card-header-copy">
              <h2>Phân loại đề xuất</h2>
              <p>Điều chỉnh loại tài liệu nếu workbook có cấu trúc hỗn hợp.</p>
            </div>
          </div>
          <div class="card-body classification-card">
            ${
              queue.length
                ? queue.map(renderClassificationItem).join("")
                : emptyState({
                    iconName: "layers",
                    title: "Chưa có file chờ xử lý",
                    description: "Sau khi chọn file, nhận diện sheet và loại tài liệu sẽ hiển thị tại đây.",
                    actionLabel: "Chọn workbook",
                    action: "focus-import-input",
                  })
            }
          </div>
        </section>
      </div>
      <section class="card" style="margin-top:18px">
        <div class="card-header">
          <div class="card-header-copy">
            <h2>Workbook đã nhập</h2>
            <p>${state.sources.length ? `${formatNumber(state.sources.length)} nguồn được lưu trong operational database.` : "Chưa có nguồn nào trong operational database."}</p>
          </div>
          <span class="badge badge-neutral">${formatNumber(state.sources.length)} nguồn</span>
        </div>
        ${
          state.sourceLoading
            ? `<div class="card-body">${skeletonCard()}</div>`
            : state.sources.length
              ? `<div class="table-wrap">${renderSourcesTable(state.sources)}</div>`
              : emptyState({
                  iconName: "database",
                  title: "Kho dữ liệu đang trống",
                  description: "Nhập bảng giá nhà cung cấp, bảng nhân công hoặc báo giá lịch sử để mở khóa matching.",
                  actionLabel: "Nhập dữ liệu",
                  action: "focus-import-input",
                })
        }
      </section>
    `;
    bindDropzone();
  }

  function renderSelectedFile(file, index) {
    return `
      <div class="selected-file">
        <span class="selected-file-icon">${icon("file")}</span>
        <span class="selected-file-main">
          <span class="selected-file-name">${escapeHtml(file.name)}</span>
          <span class="selected-file-meta">${escapeHtml(fileSize(file.size))} • ${escapeHtml(file.name.split(".").pop().toUpperCase())}</span>
        </span>
        <button class="icon-button selected-file-remove" data-action="remove-import-file" data-index="${index}" aria-label="Xóa ${escapeHtml(file.name)}">${icon("x")}</button>
      </div>
    `;
  }

  function renderClassificationItem(item, index) {
    const type = canonicalSourceType(pick(item, ["confirmed_type", "detected_type", "type"], "unknown"));
    const filename = pick(item, ["filename", "file_name", "name"], "Tệp không tên");
    const sheets = pick(item, ["sheet_count", "sheets_count", "number_of_sheets"], 0);
    const rows = pick(item, ["row_count", "rows", "extracted_rows"], 0);
    const status = sourceStatus(pick(item, ["status", "processing_status"], "queued"));
    return `
      <div class="classification-item">
        <div class="classification-item-main">
          <div class="classification-filename" title="${escapeHtml(filename)}">${escapeHtml(filename)}</div>
          <div class="classification-meta">${formatNumber(sheets)} sheet • ${formatNumber(rows)} dòng • <span class="badge ${status.className}">${status.label}</span>${item.effective_date ? ` • hiệu lực ${formatDate(item.effective_date)}` : ""}${item.preview_error ? ` • <span style="color:var(--amber)">${escapeHtml(item.preview_error)}</span>` : ""}</div>
        </div>
        <select class="select-input classification-select" data-action="classification-change" data-index="${index}" data-filename="${escapeHtml(filename)}" aria-label="Loại tài liệu của ${escapeHtml(filename)}">
          ${SOURCE_TYPES.map(([value, label]) => `<option value="${value}" ${value === String(type) ? "selected" : ""}>${label}</option>`).join("")}
        </select>
      </div>
    `;
  }

  function canonicalSourceType(type) {
    const value = String(type || "").trim().toUpperCase();
    const aliases = {
      SUPPLIER_PRICE: "supplier_price_list",
      SUPPLIER_PRICE_LIST: "supplier_price_list",
      LABOR: "labor",
      HISTORICAL_BOQ: "historical_boq",
      BOQ: "historical_boq",
      NEW_BOQ: "new_boq",
      PANEL_BOM: "panel_bom",
      MIXED: "mixed",
      UNKNOWN: "unknown",
    };
    return aliases[value] || String(type || "unknown").toLowerCase();
  }

  function renderSourcesTable(sources) {
    return `
      <table class="data-table">
        <thead><tr><th>Workbook</th><th>Loại tài liệu</th><th>Trạng thái</th><th>Sheet</th><th>Dòng dữ liệu</th><th>Nhập lúc</th><th></th></tr></thead>
        <tbody>
          ${sources
            .map((source) => {
              const status = sourceStatus(source.lifecycleStatus || source.status);
              const lifecycleValue = String(source.lifecycleStatus || "").toLowerCase();
              const isArchived = ["archived", "archive", "inactive", "retired"].includes(lifecycleValue);
              const isSuperseded = ["superseded", "outdated", "replaced"].includes(lifecycleValue);
              return `
                <tr>
                  <td><div class="cell-strong ellipsis-cell" title="${escapeHtml(source.filename)}">${escapeHtml(source.filename)}</div>${source.warnings ? `<div class="cell-muted">${formatNumber(source.warnings)} cảnh báo</div>` : ""}</td>
                  <td><span class="badge badge-neutral">${escapeHtml(sourceTypeLabel(source.confirmedType || source.detectedType))}</span></td>
                  <td><span class="badge ${status.className}">${status.label}</span></td>
                  <td class="cell-number">${formatNumber(source.sheets)}</td>
                  <td class="cell-number">${formatNumber(source.rows)}</td>
                  <td class="cell-muted">${formatDateTime(source.uploadedAt)}</td>
                  <td>
                    <div class="table-actions">
                      <button class="icon-button" data-action="source-detail" data-id="${escapeHtml(source.id)}" aria-label="Xem chi tiết">${icon("external")}</button>
                      ${
                        isSuperseded
                          ? ""
                          : isArchived
                          ? `<button class="icon-button" data-action="restore-source" data-id="${escapeHtml(source.id)}" aria-label="Khôi phục workbook" title="Khôi phục">${icon("refresh")}</button>`
                          : `<button class="icon-button" data-action="archive-source" data-id="${escapeHtml(source.id)}" aria-label="Lưu trữ workbook" title="Lưu trữ">${icon("layers")}</button>`
                      }
                    </div>
                  </td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    `;
  }

  function bindDropzone() {
    const dropzone = $("#import-dropzone");
    const input = $("#import-file-input");
    if (!dropzone || !input) return;
    ["dragenter", "dragover"].forEach((eventName) =>
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("is-dragover");
      }),
    );
    ["dragleave", "drop"].forEach((eventName) =>
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("is-dragover");
      }),
    );
    dropzone.addEventListener("drop", (event) => {
      addImportFiles(Array.from(event.dataTransfer.files || []));
    });
    input.addEventListener("change", () => addImportFiles(Array.from(input.files || [])));
  }

  function addImportFiles(files) {
    const accepted = [];
    const rejected = [];
    files.forEach((file) => {
      const extension = file.name.toLowerCase().split(".").pop();
      if (!["xls", "xlsx"].includes(extension)) {
        rejected.push(`${file.name}: chỉ hỗ trợ .xls/.xlsx`);
      } else if (file.size > FILE_SIZE_LIMIT) {
        rejected.push(`${file.name}: vượt quá 50 MB`);
      } else if (!state.currentImportFiles.some((existing) => existing.name === file.name && existing.size === file.size)) {
        accepted.push(file);
      }
    });
    if (rejected.length) toast(rejected.join(" • "), "warning", "Một số file không được thêm");
    state.currentImportFiles = [...state.currentImportFiles, ...accepted];
    if (accepted.length) {
      state.currentImportResults = [
        ...state.currentImportResults,
        ...accepted.map((file) => ({ filename: file.name, detected_type: "unknown", status: "queued", sheet_count: 0, row_count: 0 })),
      ];
      renderImport();
      previewImportFiles(accepted);
    }
  }

  async function previewImportFiles(files) {
    if (!files.length) return;
    state.importPreviewBusy = true;
    renderImport();
    await Promise.all(
      files.map(async (file) => {
        try {
          const preview = await api.importPreview(file);
          const index = state.currentImportResults.findIndex((item) => item.filename === file.name);
          if (index >= 0) {
            state.currentImportResults[index] = {
              ...state.currentImportResults[index],
              ...preview,
              detected_type: preview.detected_type || "unknown",
              status: "ready",
              sheet_count: preview.sheets?.length || 0,
              row_count: preview.total_data_rows || 0,
            };
          }
        } catch (error) {
          const index = state.currentImportResults.findIndex((item) => item.filename === file.name);
          if (index >= 0) {
            state.currentImportResults[index] = {
              ...state.currentImportResults[index],
              status: "queued",
              preview_error: "Chưa xem trước được",
            };
          }
        }
        renderImport();
      }),
    );
    state.importPreviewBusy = false;
    renderImport();
  }

  async function submitImport() {
    if (!state.currentImportFiles.length || state.importBusy) return;
    state.importBusy = true;
    renderImport();
    const classifications = {};
    $$(".classification-select").forEach((select) => {
      const filename = select.dataset.filename;
      if (filename) classifications[filename] = select.value;
    });
    try {
      const payload = await api.importFiles(state.currentImportFiles, classifications);
      const imported = rawArray(payload, ["sources", "items", "data"]);
      state.currentImportResults = imported.length ? imported : state.currentImportFiles.map((file) => ({ filename: file.name, status: "completed" }));
      state.currentImportFiles = [];
      toast("Đã gửi workbook vào pipeline. Hệ thống sẽ lưu provenance và cảnh báo parse.", "success", "Import thành công");
      await loadSources();
    } catch (error) {
      toast(error.message, "error", "Import không thành công");
    } finally {
      state.importBusy = false;
      renderImport();
    }
  }

  async function loadQuotations() {
    state.quotationLoading = true;
    renderQuotations();
    try {
      state.quotations = (await api.quotations()).map(normalizeQuotation);
    } catch (error) {
      toast(error.message, "error", "Không tải được danh sách báo giá");
    } finally {
      state.quotationLoading = false;
      renderQuotations();
    }
  }

  function renderQuotations() {
    if (state.route !== "quotations") return;
    pageView.innerHTML = `
      ${pageIntro(
        "BÁO GIÁ",
        "Hồ sơ báo giá",
        "Mỗi hồ sơ giữ lại BOQ gốc, nguồn giá, độ tin cậy và các chỉnh sửa của kỹ sư.",
        `<button class="button button-primary button-small" data-action="new-quotation">${icon("file-plus")}<span>Tạo báo giá</span></button>`,
      )}
      <section class="card list-card">
        <div class="toolbar">
          <div class="toolbar-left">
            <label class="input-with-icon search-field">
              ${icon("search")}
              <span class="sr-only">Tìm báo giá</span>
              <input class="text-input" id="quotation-search" type="search" placeholder="Tìm theo tên dự án, khách hàng…" />
            </label>
          </div>
          <div class="toolbar-right">
            <select class="select-input filter-select" id="quotation-status-filter" aria-label="Lọc trạng thái">
              <option value="all">Tất cả trạng thái</option>
              <option value="completed">Đã tính giá</option>
              <option value="review">Cần rà soát</option>
              <option value="draft">Bản nháp</option>
              <option value="running">Đang chạy</option>
            </select>
            <button class="button button-secondary button-small" data-action="refresh-quotations">${icon("refresh")}<span>Làm mới</span></button>
          </div>
        </div>
        ${
          state.quotationLoading
            ? `<div class="card-body">${skeletonCard()}</div>`
            : state.quotations.length
              ? `<div id="quotation-list">${renderQuotationTable(state.quotations)}</div>`
              : emptyState({
                  iconName: "file-plus",
                  title: "Chưa có hồ sơ báo giá",
                  description: "Upload một BOQ mới để bắt đầu quy trình tự động matching và áp giá.",
                  actionLabel: "Tạo báo giá mới",
                  action: "new-quotation",
                })
        }
      </section>
    `;
  }

  function renderQuotationTable(items) {
    return `
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>Dự án</th><th>Khách hàng</th><th>Trạng thái</th><th>Dòng BOQ</th><th>Độ phủ</th><th>Cập nhật</th><th></th></tr></thead>
          <tbody>
            ${items
              .map((item) => {
                const status = quotationStatus(item.status);
                return `
                  <tr>
                    <td><button class="button button-quiet" style="padding:0;min-height:0;color:var(--navy);font-weight:700" data-action="open-quotation" data-id="${escapeHtml(item.id)}">${escapeHtml(item.projectName)}</button>${item.sourceFilename ? `<div class="cell-muted ellipsis-cell">${escapeHtml(item.sourceFilename)}</div>` : ""}</td>
                    <td class="cell-muted">${escapeHtml(item.customer)}</td>
                    <td><span class="badge ${status.className}">${status.label}</span></td>
                    <td class="cell-number">${formatNumber(item.totalRows)}</td>
                    <td class="cell-number">${percentage(item.coverage)}</td>
                    <td class="cell-muted">${formatDateTime(item.updatedAt || item.quotationDate)}</td>
                    <td><button class="icon-button" data-action="open-quotation" data-id="${escapeHtml(item.id)}" aria-label="Mở báo giá">${icon("chevron-right")}</button></td>
                  </tr>
                `;
              })
              .join("")}
          </tbody>
        </table>
      </div>
      <div class="table-footer"><span>${formatNumber(items.length)} hồ sơ</span><span>Dữ liệu lấy trực tiếp từ API</span></div>
    `;
  }

  function renderNewQuotation() {
    if (state.route !== "new") return;
    const files = state.newQuotationFiles;
    pageView.innerHTML = `
      ${pageIntro(
        "QUY TRÌNH 01 / 03",
        "Tạo báo giá mới",
        "Upload BOQ khách hàng, xác nhận thông tin cơ bản và để engine chạy matching theo nguồn giá hiện có.",
        `<button class="button button-secondary button-small" data-action="view-quotations">${icon("file")}<span>Hồ sơ báo giá</span></button>`,
      )}
      <div class="stepper" aria-label="Tiến trình tạo báo giá">
        <div class="step is-current"><span class="step-number">1</span><span>Upload BOQ</span></div>
        <span class="step-line"></span>
        <div class="step"><span class="step-number">2</span><span>Chạy định giá</span></div>
        <span class="step-line"></span>
        <div class="step"><span class="step-number">3</span><span>Rà soát &amp; xuất</span></div>
      </div>
      <div class="quotation-layout">
        <section class="card">
          <div class="card-header">
            <div class="card-header-copy">
              <h2>Thông tin hồ sơ</h2>
              <p>Các trường này giúp tìm lại báo giá và ghi audit trail.</p>
            </div>
          </div>
          <form class="card-body form-grid" id="new-quotation-form">
            <div class="form-field">
              <label class="form-label" for="project-name">Tên dự án <span aria-hidden="true">*</span></label>
              <input class="text-input" id="project-name" name="project_name" required placeholder="Ví dụ: Trại lợn Hải Hà" />
            </div>
            <div class="form-field">
              <label class="form-label" for="customer-name">Khách hàng</label>
              <input class="text-input" id="customer-name" name="customer" placeholder="Tên chủ đầu tư / tổng thầu" />
            </div>
            <div class="form-field">
              <label class="form-label" for="quotation-date">Ngày báo giá</label>
              <input class="text-input" id="quotation-date" name="quotation_date" type="date" value="${new Date().toISOString().slice(0, 10)}" />
            </div>
            <div class="form-field">
              <label class="form-label" for="pricing-policy">Chính sách giá</label>
              <select class="select-input" id="pricing-policy" name="pricing_policy">
                <option value="latest_supplier_net">Giá NCC mới nhất sau chiết khấu</option>
                <option value="latest_approved">Giá nội bộ đã duyệt mới nhất</option>
                <option value="historical_median">Trung vị 3 dự án gần nhất</option>
              </select>
            </div>
            <div class="form-field is-wide">
              <label class="form-label" for="quotation-note">Ghi chú nội bộ</label>
              <textarea class="text-area" id="quotation-note" name="note" placeholder="Ví dụ: giữ giá theo hiệu lực 01/01/2026, cần ưu tiên nguồn Cadisun…"></textarea>
            </div>
            <div class="form-field is-wide">
              <span class="form-label">BOQ đầu vào <span aria-hidden="true">*</span></span>
              <label class="file-dropzone ${state.quotationBusy ? "is-busy" : ""}" id="quotation-dropzone">
                <span class="file-dropzone-icon">${icon("file-plus")}</span>
                <strong>Kéo thả BOQ vào đây</strong>
                <p>Chọn workbook có mô tả, mã (nếu có), đơn vị và khối lượng. Hệ thống vẫn giữ raw row để audit.</p>
                <span class="button button-secondary button-small">Chọn BOQ</span>
                <input id="quotation-file-input" type="file" accept=".xlsx,.xls" multiple ${state.quotationBusy ? "disabled" : ""} />
              </label>
              ${
                files.length
                  ? `<div class="selected-files">${files.map(renderSelectedQuotationFile).join("")}</div>`
                  : `<div class="form-hint">Có thể upload một workbook BOQ; nhiều file sẽ được lưu cùng một hồ sơ.</div>`
              }
            </div>
            <div class="form-field is-wide">
              <div id="quotation-form-error" class="callout callout-warning hidden" role="alert">${icon("alert")}<span></span></div>
            </div>
            <div class="form-field is-wide" style="display:flex;justify-content:flex-end;gap:8px">
              <button type="button" class="button button-secondary" data-action="cancel-new">Hủy</button>
              <button type="submit" class="button button-primary button-large" ${state.quotationBusy ? "disabled" : ""}>${state.quotationBusy ? "Đang tạo hồ sơ…" : "Tạo & chạy định giá"} ${icon("arrow-right")}</button>
            </div>
          </form>
        </section>
        <aside class="card">
          <div class="card-header">
            <div class="card-header-copy">
              <h2>Engine sẽ làm gì?</h2>
              <p>Giá luôn đi kèm nguồn; dòng không chắc chắn sẽ chuyển sang review.</p>
            </div>
          </div>
          <ol class="prose-list">
            <li>Đọc cấu trúc workbook và phát hiện header / cột mô tả.</li>
            <li>Chuẩn hóa mã, quy cách, đơn vị và thuộc tính kỹ thuật.</li>
            <li>Tìm candidate theo mã, alias, keyword và thuộc tính.</li>
            <li>Áp giá vật tư và nhân công theo chính sách đã chọn.</li>
            <li>Đánh dấu <strong>REVIEW_REQUIRED</strong> nếu confidence thấp hoặc thiếu nguồn.</li>
          </ol>
          <div class="card-body" style="padding-top:0">
            <div class="callout callout-success">${icon("check-circle")}<span><strong>Audit đầy đủ:</strong> file, sheet, dòng và ngày hiệu lực được giữ trong kết quả.</span></div>
          </div>
        </aside>
      </div>
    `;
    bindQuotationDropzone();
  }

  function renderSelectedQuotationFile(file, index) {
    return `
      <div class="selected-file">
        <span class="selected-file-icon">${icon("file")}</span>
        <span class="selected-file-main">
          <span class="selected-file-name">${escapeHtml(file.name)}</span>
          <span class="selected-file-meta">${escapeHtml(fileSize(file.size))}</span>
        </span>
        <button class="icon-button selected-file-remove" data-action="remove-quotation-file" data-index="${index}" aria-label="Xóa ${escapeHtml(file.name)}">${icon("x")}</button>
      </div>
    `;
  }

  function bindQuotationDropzone() {
    const dropzone = $("#quotation-dropzone");
    const input = $("#quotation-file-input");
    if (!dropzone || !input) return;
    ["dragenter", "dragover"].forEach((eventName) =>
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("is-dragover");
      }),
    );
    ["dragleave", "drop"].forEach((eventName) =>
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("is-dragover");
      }),
    );
    dropzone.addEventListener("drop", (event) => addQuotationFiles(Array.from(event.dataTransfer.files || [])));
    input.addEventListener("change", () => addQuotationFiles(Array.from(input.files || [])));
  }

  function addQuotationFiles(files) {
    const accepted = [];
    const rejected = [];
    files.forEach((file) => {
      const extension = file.name.toLowerCase().split(".").pop();
      if (!["xls", "xlsx"].includes(extension)) rejected.push(`${file.name}: chỉ hỗ trợ .xls/.xlsx`);
      else if (file.size > FILE_SIZE_LIMIT) rejected.push(`${file.name}: vượt quá 50 MB`);
      else accepted.push(file);
    });
    if (rejected.length) toast(rejected.join(" • "), "warning", "Một số file không được thêm");
    state.newQuotationFiles = [...state.newQuotationFiles, ...accepted];
    renderNewQuotation();
  }

  async function submitQuotation(form) {
    const errorBox = $("#quotation-form-error");
    const showError = (message) => {
      if (!errorBox) return;
      errorBox.classList.remove("hidden");
      const span = $("span", errorBox);
      if (span) span.textContent = message;
    };
    if (!state.newQuotationFiles.length) {
      showError("Hãy chọn ít nhất một workbook BOQ trước khi tiếp tục.");
      return;
    }
    const formData = new FormData(form);
    const fields = Object.fromEntries(formData.entries());
    state.quotationBusy = true;
    renderNewQuotation();
    try {
      const payload = await api.createQuotation(fields, state.newQuotationFiles);
      const quotation = normalizeQuotation(payload?.quotation || payload?.data || payload || {});
      if (!quotation.id) {
        state.quotationBusy = false;
        toast("Đã tạo hồ sơ nhưng API chưa trả về mã báo giá.", "warning", "Cần kiểm tra phản hồi");
        navigate("#quotations");
        return;
      }
      toast("Hồ sơ đã tạo. Engine bắt đầu tìm matching theo nguồn dữ liệu.", "success", "Đang chạy định giá");
      state.newQuotationFiles = [];
      state.quotationBusy = false;
      navigate(`#quotation/${encodeURIComponent(quotation.id)}`);
    } catch (error) {
      state.quotationBusy = false;
      renderNewQuotation();
      showError(error.message);
      toast(error.message, "error", "Không tạo được báo giá");
    }
  }

  async function loadQuotation(id, autoRun = false) {
    state.quotationLoading = true;
    state.route = "quotation";
    setBreadcrumb("quotation");
    renderQuotationDetail();
    try {
      const payload = await api.quotation(id);
      state.activeQuotation = normalizeQuotation(payload?.quotation || payload?.data || payload);
      state.activeQuotation.raw = payload;
      if (autoRun || ["draft", "queued"].includes(String(state.activeQuotation.status).toLowerCase())) {
        await runQuotation(id, false);
      }
    } catch (error) {
      state.lastError = error.message;
      toast(error.message, "error", "Không tải được báo giá");
    } finally {
      state.quotationLoading = false;
      renderQuotationDetail();
    }
  }

  async function runQuotation(id, notify = true) {
    if (!id) return;
    state.quotationBusy = true;
    renderQuotationDetail();
    try {
      const payload = await api.runQuotation(id);
      const quotation = normalizeQuotation(payload?.quotation || payload?.data || payload || {});
      state.activeQuotation = { ...(state.activeQuotation || {}), ...quotation, raw: payload };
      if (notify) toast("Pipeline đã hoàn tất hoặc đang được xử lý ở máy chủ.", "success", "Đã gửi yêu cầu định giá");
      await loadReviewItems(id, false);
      // Refresh the server-side semantic budget counter after a pricing run.
      // The API returns aggregate usage only; no key, prompt, or candidate
      // provenance is exposed to the browser.
      await loadAIHealth();
    } catch (error) {
      toast(error.message, "error", "Không chạy được định giá");
    } finally {
      state.quotationBusy = false;
      renderQuotationDetail();
    }
  }

  function renderQuotationDetail() {
    if (state.route !== "quotation") return;
    const item = state.activeQuotation;
    if (!item) {
      pageView.innerHTML = `${pageIntro("BÁO GIÁ", "Đang tải hồ sơ", "Đang lấy thông tin từ máy chủ…")}${skeletonCard()}`;
      return;
    }
    const status = quotationStatus(item.status);
    const rawStats = item.raw?.stats || item.raw?.summary || {};
    const materialMatched = pick(item, ["materialMatched"], pick(rawStats, ["material_matched"], 0));
    const laborMatched = pick(item, ["laborMatched"], pick(rawStats, ["labor_matched"], 0));
    const reviewCount = pick(item, ["reviewCount"], pick(rawStats, ["review_required", "review_count"], 0));
    const noMatch = pick(item, ["noMatch"], pick(rawStats, ["no_match", "no_match_count"], 0));
    const totalRows = pick(item, ["totalRows"], pick(rawStats, ["total_rows", "total"], 0));
    pageView.innerHTML = `
      <div class="result-hero">
        <div class="result-hero-main">
          <p class="eyebrow">CHI TIẾT BÁO GIÁ</p>
          <h1 title="${escapeHtml(item.projectName)}">${escapeHtml(item.projectName)}</h1>
          <p style="margin:0;color:var(--ink-soft)">${escapeHtml(item.customer)}${item.quotationDate ? ` • ngày ${formatDate(item.quotationDate)}` : ""} ${item.sourceFilename ? `• ${escapeHtml(item.sourceFilename)}` : ""}</p>
        </div>
        <div class="result-actions">
          <span class="badge ${status.className}">${status.label}</span>
          <button class="button button-secondary button-small" data-action="review-quotation" data-id="${escapeHtml(item.id)}">${icon("pen")}<span>Rà soát</span></button>
          <button class="button button-primary button-small" data-action="export-quotation" data-id="${escapeHtml(item.id)}">${icon("download")}<span>Xuất Excel</span></button>
        </div>
      </div>
      <div class="result-stat-grid">
        <div class="result-stat"><div class="result-stat-label">Tổng dòng BOQ</div><div class="result-stat-value">${formatNumber(totalRows)}</div></div>
        <div class="result-stat is-success"><div class="result-stat-label">Đã match vật tư</div><div class="result-stat-value">${formatNumber(materialMatched)}</div></div>
        <div class="result-stat is-success"><div class="result-stat-label">Đã match nhân công</div><div class="result-stat-value">${formatNumber(laborMatched)}</div></div>
        <div class="result-stat is-warning"><div class="result-stat-label">Cần rà soát</div><div class="result-stat-value">${formatNumber(reviewCount)}</div></div>
        <div class="result-stat is-danger"><div class="result-stat-label">Chưa có nguồn giá</div><div class="result-stat-value">${formatNumber(noMatch)}</div></div>
      </div>
      <div class="callout ${reviewCount ? "callout-warning" : "callout-success"}" style="margin-bottom:18px">
        ${icon(reviewCount ? "alert" : "check-circle")}
        <span>
          ${
            reviewCount
              ? `<strong>${formatNumber(reviewCount)} dòng cần kỹ sư quyết định.</strong> Hệ thống không tự áp giá khi confidence thấp hoặc provenance chưa đủ.`
              : `<strong>Không còn dòng rủi ro trong summary.</strong> Bạn vẫn có thể mở bàn rà soát để kiểm tra audit trước khi xuất.`
          }
        </span>
      </div>
      <div class="dashboard-grid">
        <section class="card">
          <div class="card-header">
            <div class="card-header-copy"><h2>Tóm tắt định giá</h2><p>Thông tin lấy từ pricing run gần nhất.</p></div>
            <button class="button button-secondary button-small" data-action="rerun-quotation" data-id="${escapeHtml(item.id)}" ${state.quotationBusy ? "disabled" : ""}>${icon("refresh")}<span>${state.quotationBusy ? "Đang chạy…" : "Chạy lại"}</span></button>
          </div>
          <div class="card-body">
            <dl class="quotation-summary">
              <div class="summary-row"><dt>Độ phủ tự động</dt><dd>${percentage(item.coverage)}</dd></div>
              <div class="summary-row"><dt>Giá trị dự kiến</dt><dd>${formatCurrency(item.totalValue)}</dd></div>
              <div class="summary-row"><dt>Chính sách giá</dt><dd>${escapeHtml(pick(item.raw || {}, ["pricing_policy", "policy"], "Theo cấu hình hệ thống"))}</dd></div>
              <div class="summary-row"><dt>Cập nhật lần cuối</dt><dd>${formatDateTime(item.updatedAt || item.raw?.completed_at)}</dd></div>
            </dl>
          </div>
        </section>
        <section class="card">
          <div class="card-header"><div class="card-header-copy"><h2>Tiếp theo</h2><p>Hoàn thiện các exception trước khi gửi khách hàng.</p></div></div>
          <div class="quick-actions">
            <button class="quick-action" data-action="review-quotation" data-id="${escapeHtml(item.id)}"><span class="quick-action-icon">${icon("alert")}</span><span><strong>Mở bàn rà soát</strong><span>${formatNumber(reviewCount)} dòng đang chờ quyết định.</span></span></button>
            <button class="quick-action" data-action="export-quotation" data-id="${escapeHtml(item.id)}"><span class="quick-action-icon">${icon("download")}</span><span><strong>Xuất workbook</strong><span>Thêm sheet AI Audit và provenance.</span></span></button>
          </div>
        </section>
      </div>
    `;
  }

  async function loadReviewItems(id, render = true) {
    try {
      state.reviewItems = (await api.reviewItems(id)).map(normalizeReviewItem);
      if (!state.selectedReviewId && state.reviewItems.length) state.selectedReviewId = state.reviewItems[0].id;
      if (render) renderReview();
    } catch (error) {
      if (render) toast(error.message, "error", "Không tải được các dòng cần rà soát");
    }
  }

  async function openReview(id) {
    state.route = "review";
    state.activeQuotation = state.activeQuotation?.id === id ? state.activeQuotation : null;
    state.reviewItems = [];
    state.selectedReviewId = null;
    setBreadcrumb("review");
    renderReview();
    await Promise.all([
      state.activeQuotation ? Promise.resolve() : api.quotation(id).then((payload) => (state.activeQuotation = normalizeQuotation(payload?.quotation || payload?.data || payload))),
      loadReviewItems(id, false),
    ]);
    renderReview();
  }

  function filteredReviewItems() {
    const query = state.reviewQuery.trim().toLowerCase();
    return state.reviewItems.filter((item) => {
      const matchesFilter =
        state.reviewFilter === "all" ||
        (state.reviewFilter === "review" && ["review_required", "review", "ambiguous", "external_quotation_required", "needs_supplier_quotation"].includes(String(item.status).toLowerCase())) ||
        (state.reviewFilter === "no_price" && ["no_match", "no_price_found", "unmatched"].includes(String(item.status).toLowerCase())) ||
        (state.reviewFilter === "approved" && ["approved", "auto_approved", "matched"].includes(String(item.status).toLowerCase()));
      const haystack = `${item.rawDescription} ${item.code} ${item.section} ${item.normalizedDescription}`.toLowerCase();
      return matchesFilter && (!query || haystack.includes(query));
    });
  }

  function renderReview() {
    if (state.route !== "review") return;
    const quotation = state.activeQuotation || {};
    const items = filteredReviewItems();
    const selected = state.reviewItems.find((item) => String(item.id) === String(state.selectedReviewId)) || items[0] || null;
    if (selected && state.selectedReviewId !== selected.id) state.selectedReviewId = selected.id;
    pageView.innerHTML = `
      ${pageIntro(
        "BÀN RÀ SOÁT",
        quotation.projectName || "Rà soát các dòng BOQ",
        "Tập trung vào các dòng có rủi ro. Mọi lựa chọn và chỉnh sửa sẽ được gửi về audit trail.",
        `<button class="button button-secondary button-small" data-action="open-quotation" data-id="${escapeHtml(quotation.id || "")}">${icon("chevron-right")}<span>Về summary</span></button>`,
      )}
      <div class="review-layout">
        <section class="card review-list-card">
          <div class="toolbar">
            <div class="toolbar-left">
              <label class="input-with-icon search-field">
                ${icon("search")}
                <span class="sr-only">Tìm dòng BOQ</span>
                <input class="text-input" id="review-search" type="search" placeholder="Tìm mô tả, mã, nhóm…" value="${escapeHtml(state.reviewQuery)}" />
              </label>
            </div>
            <div class="toolbar-right">
              <select class="select-input filter-select" id="review-filter" aria-label="Lọc dòng rà soát">
                <option value="review" ${state.reviewFilter === "review" ? "selected" : ""}>Cần rà soát</option>
                <option value="no_price" ${state.reviewFilter === "no_price" ? "selected" : ""}>Chưa có giá</option>
                <option value="approved" ${state.reviewFilter === "approved" ? "selected" : ""}>Đã duyệt</option>
                <option value="all" ${state.reviewFilter === "all" ? "selected" : ""}>Tất cả dòng</option>
              </select>
            </div>
          </div>
          <div class="review-list">
            ${
              items.length
                ? items.map((item) => renderReviewListItem(item, selected?.id)).join("")
                : emptyState({
                    iconName: "check-circle",
                    title: state.reviewItems.length ? "Không có dòng phù hợp" : "Chưa có dữ liệu rà soát",
                    description: state.reviewItems.length ? "Thử đổi bộ lọc hoặc từ khóa tìm kiếm." : "Chạy pricing run để tạo danh sách candidate và cảnh báo.",
                    actionLabel: state.reviewItems.length ? "Xóa bộ lọc" : "Về summary",
                    action: state.reviewItems.length ? "clear-review-filter" : "open-quotation",
                  })
            }
          </div>
          <div class="table-footer"><span>${formatNumber(items.length)} / ${formatNumber(state.reviewItems.length)} dòng</span><span>Chọn một dòng để xem provenance</span></div>
        </section>
        <section class="card review-detail">
          ${selected ? renderReviewDetail(selected) : emptyState({ iconName: "info", title: "Chọn một dòng để bắt đầu", description: "Candidate, confidence và nguồn giá sẽ hiển thị tại đây." })}
        </section>
      </div>
    `;
  }

  function renderReviewListItem(item, selectedId) {
    const status = reviewStatus(item.status);
    const isNoMatch = ["no_match", "no_price_found", "unmatched"].includes(String(item.status).toLowerCase());
    const isOk = ["approved", "auto_approved", "matched"].includes(String(item.status).toLowerCase());
    const confidence = item.materialConfidence ?? item.laborConfidence;
    return `
      <button class="review-item ${String(item.id) === String(selectedId) ? "is-selected" : ""} ${isNoMatch ? "is-no-match" : ""} ${isOk ? "is-ok" : ""}" data-action="select-review" data-id="${escapeHtml(item.id)}">
        <span class="review-risk">${icon(isNoMatch ? "x" : isOk ? "check" : "alert")}</span>
        <span class="review-item-main">
          <span class="review-item-title">${escapeHtml(item.rawDescription)}</span>
          <span class="review-item-meta"><span>${escapeHtml(item.code || "Chưa có mã")}</span><span>${escapeHtml(item.unit || "—")}</span><span class="badge ${status.className}">${status.label}</span></span>
        </span>
        <span class="review-item-confidence">${confidence === null || confidence === undefined ? "—" : percentage(confidence)}</span>
      </button>
    `;
  }

  function renderReviewDetail(item) {
    const recommendation = item.recommendation || {};
    const candidates = item.candidates.length ? item.candidates : recommendation.id || recommendation.name ? [recommendation] : [];
    const status = reviewStatus(item.status);
    return `
      <div class="detail-section">
        <div class="detail-heading"><h3>Raw description</h3><span class="badge ${status.className}">${status.label}</span></div>
        <div class="raw-description">${escapeHtml(item.rawDescription)}</div>
        ${item.explanation ? `<div class="callout callout-warning" style="margin-top:10px">${icon("alert")}<span>${escapeHtml(item.explanation)}</span></div>` : ""}
      </div>
      <div class="detail-section">
        <div class="detail-heading"><h3>Thuộc tính đã chuẩn hóa</h3></div>
        <div class="attribute-grid">
          ${attribute("Mô tả chuẩn", item.normalizedDescription || "Chưa có")}
          ${attribute("Mã vật tư", item.code || "Chưa có")}
          ${attribute("Nhóm", item.section || "Chưa có")}
          ${attribute("Đơn vị", item.unit || "Chưa có")}
          ${attribute("Khối lượng", formatNumber(item.quantity))}
          ${attribute("Hãng / xuất xứ", [item.brand, item.origin].filter(Boolean).join(" • ") || "Chưa có")}
        </div>
      </div>
      <div class="detail-section">
        <div class="detail-heading"><h3>Candidate đề xuất</h3><span class="cell-muted">${formatNumber(candidates.length)} lựa chọn</span></div>
        ${
          candidates.length
            ? `<div class="candidate-list">${candidates.slice(0, 6).map((candidate, index) => renderCandidate(candidate, index === 0)).join("")}</div>`
          : emptyState({ iconName: "search", title: "Chưa tìm thấy ứng viên phù hợp", description: "Không có ứng viên vật tư hoặc nhân công đủ độ tin cậy. Bạn có thể nhập giá kèm provenance hoặc yêu cầu báo giá nhà cung cấp." })
        }
      </div>
      <div class="detail-section">
        <div class="detail-heading"><h3>Provenance giá</h3></div>
        <div class="price-provenance">
          ${provenanceRow("Vật tư", item.materialPrice === null ? "Chưa có giá" : formatCurrency(item.materialPrice))}
          ${provenanceRow("Nguồn vật tư", provenanceSource(item.materialSource))}
          ${provenanceRow("Confidence vật tư", item.materialConfidence === null ? "—" : percentage(item.materialConfidence))}
          ${provenanceRow("Nhân công", item.laborPrice === null ? "Chưa có giá" : formatCurrency(item.laborPrice))}
          ${provenanceRow("Nguồn nhân công", provenanceSource(item.laborSource))}
          ${provenanceRow("Confidence nhân công", item.laborConfidence === null ? "—" : percentage(item.laborConfidence))}
        </div>
      </div>
      <div class="detail-section">
        <div class="review-actions">
          <button class="button button-primary" data-action="approve-review" data-id="${escapeHtml(item.id)}">${icon("check")}<span>Duyệt đề xuất hiện tại</span></button>
          <button class="button button-secondary" data-action="choose-candidate" data-id="${escapeHtml(item.id)}">${icon("layers")}<span>Chọn candidate khác</span></button>
          <button class="button button-secondary" data-action="manual-price" data-id="${escapeHtml(item.id)}">${icon("pen")}<span>Nhập giá thủ công</span></button>
          <button class="button button-secondary" data-action="supplier-quote" data-id="${escapeHtml(item.id)}">${icon("alert")}<span>Yêu cầu báo giá NCC</span></button>
          <button class="button button-quiet" data-action="ignore-review" data-id="${escapeHtml(item.id)}">${icon("x")}<span>Bỏ qua dòng</span></button>
        </div>
      </div>
    `;
  }

  function attribute(label, value) {
    return `<div class="attribute"><span class="attribute-label">${escapeHtml(label)}</span><span class="attribute-value" title="${escapeHtml(value)}">${escapeHtml(value)}</span></div>`;
  }

  function renderCandidate(candidate, recommended = false) {
    const name = pick(candidate, ["name", "normalized_name", "description", "product_name"], "Candidate không tên");
    const code = pick(candidate, ["code", "product_code", "id"], "Không có mã");
    const confidence = pick(candidate, ["confidence", "score", "similarity"], null);
    return `
      <div class="candidate ${recommended ? "is-recommended" : ""}">
        <div><div class="candidate-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div><div class="candidate-meta">${escapeHtml(code)}${candidate.unit ? ` • ${escapeHtml(candidate.unit)}` : ""}</div></div>
        <div class="candidate-confidence">${confidence === null ? "—" : percentage(confidence)}</div>
      </div>
    `;
  }

  function provenanceSource(source) {
    if (!source || typeof source !== "object") return source ? escapeHtml(source) : "Chưa có nguồn";
    const value = pick(source, ["name", "source", "filename", "file_name", "supplier"], "");
    const date = pick(source, ["effective_date", "date"], "");
    const row = pick(source, ["row", "source_row", "line"], "");
    return escapeHtml([value, date && `hiệu lực ${formatDate(date)}`, row && `dòng ${row}`].filter(Boolean).join(" • ") || "Chưa có nguồn");
  }

  function provenanceRow(label, value) {
    return `<div class="provenance-row"><span class="provenance-label">${escapeHtml(label)}</span><span class="provenance-value" title="${escapeHtml(String(value))}">${value}</span></div>`;
  }

  async function reviewAction(action, id) {
    const item = state.reviewItems.find((entry) => String(entry.id) === String(id));
    if (!item) return;
    let payload = { action };
    if (action === "approve") {
      const selected = item.recommendation || {};
      const candidateType = candidateKind(selected);
      payload =
        candidateType === "labor"
          ? { action: "approve", selected_labor_item_id: selected.id || selected.labor_item_id || null }
          : { action: "approve", selected_product_id: selected.id || selected.product_id || null };
    } else if (action === "ignore") {
      payload = { action: "ignore" };
    } else if (action === "supplier_quote") {
      payload = { action: "needs_supplier_quotation" };
    } else if (action === "choose_candidate") {
      const choice = await openCandidateModal(item);
      if (!choice) return;
      payload = {
        action: "select_candidate",
        ...(candidateKind(choice) === "labor"
          ? { selected_labor_item_id: choice.id }
          : { selected_product_id: choice.id }),
      };
    } else if (action === "manual_price") {
      const values = await openManualPriceModal(item);
      if (!values) return;
      payload = { action: "manual_price", material_price: values.material, labor_price: values.labor, note: values.note };
    }
    try {
      await api.reviewBOQItem(item.id, payload);
      toast("Đã lưu quyết định và provenance của dòng BOQ.", "success", "Cập nhật thành công");
      await loadReviewItems(state.activeQuotation?.id, true);
    } catch (error) {
      toast(error.message, "error", "Không lưu được chỉnh sửa");
    }
  }

  function openCandidateModal(item) {
    return new Promise((resolve) => {
      const fallback = item.recommendation ? [item.recommendation] : [];
      const candidates = item.candidates.length ? item.candidates : fallback;
      modalRoot.innerHTML = `
        <div class="modal-backdrop" data-modal-dismiss>
          <section class="modal" role="dialog" aria-modal="true" aria-labelledby="candidate-modal-title">
            <div class="modal-header"><div><h2 id="candidate-modal-title">Chọn candidate</h2><p style="margin:4px 0 0;color:var(--ink-muted);font-size:11px">${escapeHtml(item.rawDescription)}</p></div><button class="icon-button" data-modal-dismiss aria-label="Đóng">${icon("x")}</button></div>
            <div class="modal-body">
              <div class="candidate-list">
                ${
                  candidates.length
                    ? candidates.map((candidate, index) => {
                        const candidateType = candidateKind(candidate);
                        const candidateId = pick(candidate, ["id", "candidate_id", "product_id", "labor_item_id", "labor_id", "code"], "");
                        const candidateCode = pick(candidate, ["code", "product_code", "labor_code"], "Không có mã");
                        return `<label class="candidate ${index === 0 ? "is-recommended" : ""}" style="cursor:pointer"><span><input type="radio" name="candidate-choice" value="${escapeHtml(candidateId)}" data-type="${escapeHtml(candidateType)}" ${index === 0 ? "checked" : ""} style="margin-right:8px"><span class="candidate-name">${escapeHtml(pick(candidate, ["name", "description", "normalized_name", "product_name"], "Candidate không tên"))}</span><span class="candidate-meta">${escapeHtml(candidateCode)} • ${candidateType === "labor" ? "Nhân công" : "Vật tư"}</span></span><span class="candidate-confidence">${pick(candidate, ["confidence", "score", "similarity"], null) === null ? "—" : percentage(pick(candidate, ["confidence", "score", "similarity"], null))}</span></label>`;
                      }).join("")
                    : `<div class="callout callout-warning">${icon("info")}<span>Chưa có ứng viên vật tư hoặc nhân công để chọn. Hãy nhập giá kèm nguồn hoặc đánh dấu cần báo giá nhà cung cấp.</span></div>`
                }
              </div>
            </div>
            <div class="modal-footer"><button class="button button-secondary" data-modal-dismiss>Hủy</button><button class="button button-primary" data-modal-confirm="candidate">Lưu lựa chọn</button></div>
          </section>
        </div>
      `;
      modalRoot.querySelector("[data-modal-confirm='candidate']")?.addEventListener("click", () => {
        const selected = modalRoot.querySelector("input[name='candidate-choice']:checked");
        modalRoot.innerHTML = "";
        resolve(selected ? { id: selected.value, candidate_type: selected.dataset.type || "material" } : null);
      });
      bindModalDismiss(() => resolve(null));
    });
  }

  function openManualPriceModal(item) {
    return new Promise((resolve) => {
      modalRoot.innerHTML = `
        <div class="modal-backdrop" data-modal-dismiss>
          <section class="modal" role="dialog" aria-modal="true" aria-labelledby="manual-price-title">
            <div class="modal-header"><div><h2 id="manual-price-title">Nhập giá có nguồn</h2><p style="margin:4px 0 0;color:var(--ink-muted);font-size:11px">${escapeHtml(item.rawDescription)}</p></div><button class="icon-button" data-modal-dismiss aria-label="Đóng">${icon("x")}</button></div>
            <form class="modal-body form-grid" id="manual-price-form">
              <div class="form-field"><label class="form-label" for="manual-material">Giá vật tư (₫)</label><input class="text-input" id="manual-material" name="material" type="number" min="0" step="1" value="${item.materialPrice ?? ""}" placeholder="Bắt buộc nếu có giá" /></div>
              <div class="form-field"><label class="form-label" for="manual-labor">Giá nhân công (₫)</label><input class="text-input" id="manual-labor" name="labor" type="number" min="0" step="1" value="${item.laborPrice ?? ""}" placeholder="Bắt buộc nếu có giá" /></div>
              <div class="form-field is-wide"><label class="form-label" for="manual-note">Nguồn / ghi chú</label><textarea class="text-area" id="manual-note" name="note" required placeholder="Ví dụ: email NCC ngày 05/09/2026, file báo giá…"></textarea></div>
              <div class="form-field is-wide"><div class="callout callout-warning">${icon("info")}<span>Không nên nhập giá nếu không có tài liệu hoặc ghi chú nguồn. Audit sẽ lưu người thực hiện và thời điểm.</span></div></div>
            </form>
            <div class="modal-footer"><button class="button button-secondary" data-modal-dismiss>Hủy</button><button class="button button-primary" data-modal-confirm="manual">Lưu giá</button></div>
          </section>
        </div>
      `;
      modalRoot.querySelector("[data-modal-confirm='manual']")?.addEventListener("click", () => {
        const form = $("#manual-price-form", modalRoot);
        if (!form.reportValidity()) return;
        const values = Object.fromEntries(new FormData(form).entries());
        modalRoot.innerHTML = "";
        resolve(values);
      });
      bindModalDismiss(() => resolve(null));
    });
  }

  function bindModalDismiss(onDismiss) {
    modalRoot.querySelectorAll("[data-modal-dismiss]").forEach((element) => {
      element.addEventListener("click", (event) => {
        if (event.target !== element && element.classList.contains("modal-backdrop")) return;
        modalRoot.innerHTML = "";
        onDismiss();
      });
    });
    const escapeHandler = (event) => {
      if (event.key === "Escape" && modalRoot.innerHTML) {
        modalRoot.innerHTML = "";
        document.removeEventListener("keydown", escapeHandler);
        onDismiss();
      }
    };
    document.addEventListener("keydown", escapeHandler);
  }

  async function loadCatalog() {
    state.catalogLoading = true;
    renderCatalog();
    const [statsResult, sourcesResult] = await Promise.allSettled([api.catalogStats(), api.sources()]);
    if (statsResult.status === "fulfilled") state.catalogStats = statsResult.value || {};
    if (sourcesResult.status === "fulfilled") state.sources = sourcesResult.value.map(normalizeSource);
    await loadCatalogItems(false);
    state.catalogLoading = false;
    renderCatalog();
  }

  function normalizeCatalogPayload(payload) {
    const nestedCatalog = payload?.catalog || payload?.data?.catalog || {};
    const nestedData = payload?.data && typeof payload.data === "object" ? payload.data : {};
    const facets = payload?.facets || nestedData.facets || nestedCatalog.facets || {};
    const rawItems = rawArray(payload, ["items", "records", "data"]).length
      ? rawArray(payload, ["items", "records", "data"])
      : rawArray(nestedData, ["items", "records"]);
    const fallbackItems = rawItems.length
      ? rawItems
      : [
          ...rawArray(payload, ["products", "product_records"]).map((item) => ({ ...item, kind: "product" })),
          ...rawArray(payload, ["labor", "labor_items", "labor_records"]).map((item) => ({ ...item, kind: "labor" })),
          ...rawArray(nestedCatalog, ["products", "product_records"]).map((item) => ({ ...item, kind: "product" })),
          ...rawArray(nestedCatalog, ["labor", "labor_items", "labor_records"]).map((item) => ({ ...item, kind: "labor" })),
        ];
    const items = fallbackItems.map((item) => normalizeCatalogItem(item, state.catalogFilters.kind));
    const categories = rawArray(payload, ["categories", "category_facets", "facets_categories"]).length
      ? rawArray(payload, ["categories", "category_facets", "facets_categories"])
      : rawArray(facets, ["categories", "category"]);
    const sources = rawArray(payload, ["sources", "source_facets", "facets_sources"]).length
      ? rawArray(payload, ["sources", "source_facets", "facets_sources"])
      : rawArray(facets, ["sources", "source"]);
    const statuses = rawArray(payload, ["statuses", "status_facets", "facets_statuses"]).length
      ? rawArray(payload, ["statuses", "status_facets", "facets_statuses"])
      : rawArray(facets, ["statuses", "status"]);
    return {
      items,
      total: countValue(pick(payload, ["total", "count", "total_items"], items.length), items.length),
      categories: categories.length ? categories : Array.from(new Set(items.map((item) => item.category).filter(Boolean))),
      sources: sources.length ? sources : Array.from(new Set(items.map((item) => item.sourceId).filter(Boolean))),
      statuses: statuses.length ? statuses : Array.from(new Set(items.map((item) => item.status).filter(Boolean))),
    };
  }

  function applyCatalogFilters(items) {
    const filters = state.catalogFilters;
    const query = String(filters.query || "").trim().toLowerCase();
    return items.filter((item) => {
      const kindMatches = filters.kind === "all" || item.kind === filters.kind;
      const categoryMatches = filters.category === "all" || String(item.category || "").toLowerCase() === String(filters.category).toLowerCase();
      const sourceMatches = filters.sourceId === "all" || String(item.sourceId || "") === String(filters.sourceId);
      const statusMatches = filters.status === "all" || String(item.status || "").toLowerCase() === String(filters.status).toLowerCase();
      const haystack = [item.name, item.code, item.category, item.subcategory, item.brand, item.origin, item.sourceFilename]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return kindMatches && categoryMatches && sourceMatches && statusMatches && (!query || haystack.includes(query));
    });
  }

  async function loadCatalogItems(render = true) {
    state.catalogLoading = true;
    if (render) renderCatalog();
    try {
      const payload = await api.catalogItems({
        ...state.catalogFilters,
        limit: state.catalogPageSize,
        offset: state.catalogPage * state.catalogPageSize,
      });
      const normalized = normalizeCatalogPayload(payload || {});
      state.catalogItems = payload?.__serverFiltered ? normalized.items : applyCatalogFilters(normalized.items);
      state.catalogTotal = normalized.total;
      state.catalogFacets = {
        categories: normalized.categories,
        sources: normalized.sources,
        statuses: normalized.statuses,
      };
    } catch (error) {
      state.catalogItems = [];
      state.catalogTotal = 0;
      if (render) toast(error.message, "error", "Không tải được danh sách danh mục");
    } finally {
      state.catalogLoading = false;
      if (render) renderCatalog();
    }
  }

  function catalogKindLabel(kind) {
    return kind === "labor" ? "Nhân công" : kind === "price" ? "Quan sát giá" : "Vật tư";
  }

  function catalogStatus(status) {
    const value = String(status || "").toLowerCase();
    if (["archived", "inactive", "retired"].includes(value)) return { label: "Đã lưu trữ", className: "badge-neutral" };
    if (["outdated", "superseded", "replaced"].includes(value)) return { label: "Đã thay thế", className: "badge-warning" };
    if (["draft", "pending"].includes(value)) return { label: "Nháp", className: "badge-blue" };
    return { label: "Đang dùng", className: "badge-success" };
  }

  function catalogSourceLabel(item) {
    if (item.sourceFilename) return item.sourceFilename;
    if (item.sourceId) {
      const source = state.sources.find((candidate) => String(candidate.id) === String(item.sourceId));
      if (source) return source.filename;
    }
    return "Chưa có provenance";
  }

  function catalogFacetValues(values, sourceMode = false) {
    const normalized = [];
    (values || []).forEach((value) => {
      if (value && typeof value === "object") {
        const id = pick(value, ["id", "source_id", "value", "key"], "");
        const label = pick(value, ["label", "name", "filename", "title"], id);
        if (id !== "") normalized.push({ value: String(id), label: String(label) });
      } else if (value !== null && value !== undefined && String(value) !== "") {
        const source = sourceMode && state.sources.find((item) => String(item.id) === String(value));
        normalized.push({ value: String(value), label: source?.filename || String(value) });
      }
    });
    const seen = new Set();
    return normalized.filter((item) => !seen.has(item.value) && seen.add(item.value));
  }

  function renderCatalog() {
    if (state.route !== "catalog") return;
    const products = getCatalogNumber(["products", "product_count", "materials", "material_count"], 0);
    const prices = getCatalogNumber(["prices", "price_count", "product_prices"], 0);
    const labors = getCatalogNumber(["labor_items", "labor_count", "labors"], 0);
    const rules = getCatalogNumber(["rules", "rule_count", "aliases"], 0);
    const filters = state.catalogFilters;
    const categoryOptions = catalogFacetValues(state.catalogFacets.categories);
    const sourceOptions = catalogFacetValues(
      state.catalogFacets.sources?.length ? state.catalogFacets.sources : state.sources.map((source) => ({ id: source.id, filename: source.filename })),
      true,
    );
    const statusOptions = catalogFacetValues(state.catalogFacets.statuses);
    pageView.innerHTML = `
      ${pageIntro(
        "DANH MỤC & GIÁ",
        "Operational data hub",
        "Kiểm soát từng vật tư, giá và mục nhân công đã ingest. Mỗi dòng đều có thể truy ngược về workbook, sheet và dòng nguồn.",
        `<button class="button button-secondary button-small" data-action="refresh-catalog">${icon("refresh")}<span>Làm mới danh mục</span></button>`,
      )}
      <div class="catalog-overview">
        <div class="catalog-tile"><span class="catalog-tile-icon">${icon("database")}</span><span><strong>${formatNumber(products)}</strong><span>Sản phẩm chuẩn hóa</span></span></div>
        <div class="catalog-tile"><span class="catalog-tile-icon">${icon("layers")}</span><span><strong>${formatNumber(prices)}</strong><span>Phiên bản giá</span></span></div>
        <div class="catalog-tile"><span class="catalog-tile-icon">${icon("book")}</span><span><strong>${formatNumber(labors)}</strong><span>Mục nhân công</span></span></div>
      </div>
      <section class="card catalog-browser">
        <div class="card-header">
          <div class="card-header-copy">
            <h2>Danh sách hạng mục đã nhận</h2>
            <p>${state.catalogTotal ? `${formatNumber(state.catalogTotal)} record theo bộ lọc hiện tại.` : "Lọc theo loại, nhóm hoặc nguồn để kiểm tra dữ liệu cụ thể."}</p>
          </div>
          <span class="badge badge-neutral">${formatNumber(state.catalogTotal || state.catalogItems.length)} record</span>
        </div>
        <div class="toolbar catalog-toolbar">
          <div class="toolbar-left catalog-toolbar-left">
            <label class="input-with-icon search-field">
              ${icon("search")}
              <span class="sr-only">Tìm trong danh mục</span>
              <input class="text-input" id="catalog-search" type="search" value="${escapeHtml(filters.query)}" placeholder="Tìm tên, mã, hãng, mô tả…" />
            </label>
            <select class="select-input filter-select" id="catalog-kind-filter" aria-label="Lọc loại hạng mục">
              <option value="all" ${filters.kind === "all" ? "selected" : ""}>Tất cả loại</option>
              <option value="product" ${filters.kind === "product" ? "selected" : ""}>Vật tư</option>
              <option value="labor" ${filters.kind === "labor" ? "selected" : ""}>Nhân công</option>
            </select>
          </div>
          <div class="toolbar-right catalog-toolbar-right">
            <select class="select-input filter-select" id="catalog-category-filter" aria-label="Lọc nhóm danh mục">
              <option value="all">Tất cả nhóm</option>
              ${categoryOptions.map((option) => `<option value="${escapeHtml(option.value)}" ${filters.category === option.value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}
            </select>
            <select class="select-input filter-select" id="catalog-source-filter" aria-label="Lọc workbook nguồn">
              <option value="all">Tất cả nguồn</option>
              ${sourceOptions.map((option) => `<option value="${escapeHtml(option.value)}" ${String(filters.sourceId) === option.value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}
            </select>
            <select class="select-input filter-select" id="catalog-status-filter" aria-label="Lọc trạng thái">
              <option value="all">Tất cả trạng thái</option>
              ${statusOptions.map((option) => `<option value="${escapeHtml(option.value)}" ${String(filters.status) === option.value ? "selected" : ""}>${escapeHtml(catalogStatus(option.value).label)}</option>`).join("")}
              ${statusOptions.length === 0 ? `<option value="active" ${filters.status === "active" ? "selected" : ""}>Đang dùng</option><option value="archived" ${filters.status === "archived" ? "selected" : ""}>Đã lưu trữ</option>` : ""}
            </select>
          </div>
        </div>
        ${
          state.catalogLoading
            ? `<div class="card-body">${skeletonCard()}</div>`
            : state.catalogItems.length
              ? `<div class="table-wrap">${renderCatalogTable(state.catalogItems)}</div>`
              : emptyState({
                  iconName: "database",
                  title: "Không có record phù hợp",
                  description: "Thử bỏ bớt bộ lọc hoặc nhập thêm workbook bảng giá / nhân công vào Kho dữ liệu.",
                  actionLabel: "Mở Kho dữ liệu",
                  action: "import",
                })
        }
        ${state.catalogLoading ? "" : renderCatalogPagination()}
      </section>
      <div class="dashboard-grid">
        <section class="card">
          <div class="card-header"><div class="card-header-copy"><h2>Phạm vi dữ liệu</h2><p>Thống kê trả về từ <code>/api/catalog/stats</code>.</p></div><span class="badge badge-success">Nguồn vận hành</span></div>
          <div class="card-body">
            <dl class="quotation-summary">
              <div class="summary-row"><dt>Sản phẩm / vật tư</dt><dd>${formatNumber(products)}</dd></div>
              <div class="summary-row"><dt>Bảng giá có provenance</dt><dd>${formatNumber(prices)}</dd></div>
              <div class="summary-row"><dt>Mục nhân công</dt><dd>${formatNumber(labors)}</dd></div>
              <div class="summary-row"><dt>Alias / matching rule</dt><dd>${formatNumber(rules)}</dd></div>
            </dl>
          </div>
        </section>
        <section class="card">
          <div class="card-header"><div class="card-header-copy"><h2>Nguyên tắc dữ liệu</h2><p>Để tránh áp nhầm giá trong môi trường doanh nghiệp.</p></div></div>
          <ol class="prose-list">
            <li>Không overwrite giá cũ; mỗi phiên bản có ngày hiệu lực.</li>
            <li>Giá không có file / sheet / dòng nguồn sẽ không được apply.</li>
            <li>Corrections từ bàn review có thể trở thành alias hoặc rule.</li>
          </ol>
        </section>
      </div>
      ${
        Number(products) || Number(prices) || Number(labors)
          ? `<section class="card" style="margin-top:18px"><div class="card-header"><div class="card-header-copy"><h2>Trạng thái sẵn sàng</h2><p>Danh mục có thể được dùng ngay trong pricing run tiếp theo.</p></div></div><div class="card-body"><div class="callout callout-success">${icon("check-circle")}<span>Operational database đã có dữ liệu. Hãy tạo một báo giá mới để kiểm tra coverage trên BOQ thực tế.</span></div></div></section>`
          : `<section class="card" style="margin-top:18px">${emptyState({ iconName: "database", title: "Danh mục chưa có dữ liệu", description: "Nhập bảng giá và dữ liệu lịch sử trong Import Center trước khi chạy báo giá.", actionLabel: "Mở Import Center", action: "import" })}</section>`
      }
    `;
  }

  function renderCatalogPagination() {
    const total = Math.max(0, Number(state.catalogTotal) || 0);
    const pageSize = Math.max(1, Number(state.catalogPageSize) || 100);
    const pageCount = Math.max(1, Math.ceil(total / pageSize));
    const currentPage = Math.min(Math.max(0, Number(state.catalogPage) || 0), pageCount - 1);
    const start = total ? currentPage * pageSize + 1 : 0;
    const end = total ? Math.min(total, (currentPage + 1) * pageSize) : 0;
    if (!total && currentPage === 0) return "";
    return `
      <div class="catalog-pagination" aria-label="Phân trang danh mục">
        <span class="catalog-pagination-summary">${total ? `Hiển thị ${formatNumber(start)}–${formatNumber(end)} trong ${formatNumber(total)} record` : "Không có record"}</span>
        <span class="catalog-pagination-controls">
          <button class="button button-secondary button-small" data-action="catalog-page" data-direction="prev" ${currentPage <= 0 ? "disabled" : ""}>${icon("chevron-right", "icon-rotate-180")}<span>Trước</span></button>
          <span class="catalog-pagination-page">Trang ${formatNumber(currentPage + 1)} / ${formatNumber(pageCount)}</span>
          <button class="button button-secondary button-small" data-action="catalog-page" data-direction="next" ${currentPage >= pageCount - 1 ? "disabled" : ""}><span>Sau</span>${icon("chevron-right")}</button>
        </span>
      </div>
    `;
  }

  function renderCatalogTable(items) {
    return `
      <table class="data-table catalog-table">
        <thead><tr><th>Hạng mục</th><th>Loại</th><th>Phân loại</th><th>Đơn vị</th><th>Giá hiện hành</th><th>Nguồn / hiệu lực</th><th>Trạng thái</th><th></th></tr></thead>
        <tbody>
          ${items.map((item) => {
            const status = catalogStatus(item.status);
            const price = item.currentPrice === null || item.currentPrice === undefined || item.currentPrice === ""
              ? "—"
              : item.kind === "labor"
                ? formatCurrency(item.currentPrice)
                : formatCurrency(item.currentPrice);
            const source = catalogSourceLabel(item);
            return `
              <tr>
                <td>
                  <button class="button button-quiet catalog-item-link" data-action="catalog-detail" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind)}">${escapeHtml(item.name)}</button>
                  ${item.code ? `<div class="cell-muted">${escapeHtml(item.code)}</div>` : ""}
                  ${item.brand || item.origin ? `<div class="cell-muted">${escapeHtml([item.brand, item.origin].filter(Boolean).join(" • "))}</div>` : ""}
                </td>
                <td><span class="badge badge-neutral">${escapeHtml(catalogKindLabel(item.kind))}</span></td>
                <td>${escapeHtml(item.category || "Chưa phân loại")}${item.subcategory ? `<div class="cell-muted">${escapeHtml(item.subcategory)}</div>` : ""}</td>
                <td class="cell-muted">${escapeHtml(item.unit || "—")}</td>
                <td class="cell-number">${price}${item.observationCount ? `<div class="cell-muted catalog-observation-count">${formatNumber(item.observationCount)} quan sát</div>` : ""}</td>
                <td>
                  ${
                    item.sourceId
                      ? `<button class="button button-quiet source-link" data-action="source-detail" data-id="${escapeHtml(item.sourceId)}" title="Mở workbook nguồn">${escapeHtml(source)}</button>`
                      : `<span class="cell-muted">${escapeHtml(source)}</span>`
                  }
                  ${item.effectiveDate ? `<div class="cell-muted">Hiệu lực ${formatDate(item.effectiveDate)}</div>` : ""}
                </td>
                <td><span class="badge ${status.className}">${status.label}</span></td>
                <td>
                  <div class="table-actions">
                    <button class="icon-button" data-action="catalog-detail" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind)}" aria-label="Xem chi tiết">${icon("external")}</button>
                    ${
                      ["archived", "inactive", "retired"].includes(String(item.status || "").toLowerCase())
                        ? `<button class="icon-button" data-action="restore-catalog-item" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind)}" aria-label="Khôi phục hạng mục" title="Khôi phục hạng mục">${icon("refresh")}</button>`
                        : `<button class="icon-button" data-action="archive-catalog-item" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind)}" aria-label="Lưu trữ hạng mục" title="Lưu trữ hạng mục">${icon("layers")}</button>`
                    }
                  </div>
                </td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    `;
  }

  async function loadBenchmark() {
    state.benchmarkLoading = true;
    renderBenchmark();
    try {
      state.benchmark = await api.benchmark();
    } catch (error) {
      toast(error.message, "error", "Không tải được benchmark");
    } finally {
      state.benchmarkLoading = false;
      renderBenchmark();
    }
  }

  function benchmarkValue(keys, fallback = null) {
    return pick(state.benchmark || {}, keys, fallback);
  }

  function renderBenchmark() {
    if (state.route !== "benchmark") return;
    const materialCoverage = benchmarkValue(["material_coverage", "materialCoverage", "material", "coverage"], null);
    const laborCoverage = benchmarkValue(["labor_coverage", "laborCoverage", "labor"], null);
    const parsing = benchmarkValue(["parsing_success", "parse_success", "extraction_accuracy"], null);
    const highConfidence = benchmarkValue(["high_confidence_accuracy", "accuracy", "material_accuracy"], null);
    const total = benchmarkValue(["total_items", "total_rows", "holdout_rows"], 0);
    const reportUrl = benchmarkValue(["report_url", "report"], "");
    const safeReportUrl = safeHref(reportUrl);
    pageView.innerHTML = `
      ${pageIntro("ĐÁNH GIÁ ĐỘ PHỦ", "Holdout benchmark", "Đo coverage và độ chính xác trên dữ liệu holdout, tách khỏi nguồn giá dùng để train / ingest.", `<button class="button button-secondary button-small" data-action="refresh-benchmark">${icon("refresh")}<span>Làm mới benchmark</span></button>`)}
      ${
        state.benchmarkLoading
          ? `<div class="dashboard-grid">${skeletonCard()}${skeletonCard()}</div>`
          : state.benchmark && Object.keys(state.benchmark).length
            ? `
              <div class="benchmark-layout">
                <section class="card">
                  <div class="card-header"><div class="card-header-copy"><h2>Coverage theo pipeline</h2><p>${total ? `${formatNumber(total)} dòng trong holdout.` : "Số liệu từ lần chạy benchmark gần nhất."}</p></div><span class="badge badge-blue">Không fake dữ liệu</span></div>
                  <div class="coverage-chart">
                    ${coverageRow("Parse usable", parsing, "")}
                    ${coverageRow("Vật tư tự động", materialCoverage, "")}
                    ${coverageRow("Nhân công tự động", laborCoverage, "is-warning")}
                    ${coverageRow("Đúng ở confidence cao", highConfidence, "is-success")}
                  </div>
                </section>
                <section class="card">
                  <div class="card-header"><div class="card-header-copy"><h2>Đọc kết quả</h2><p>Ưu tiên kiểm soát false-positive hơn việc tăng coverage mù quáng.</p></div></div>
                  <ol class="prose-list">
                    <li>Độ phủ thấp thường đến từ thiếu mã, thiếu điện áp hoặc quy cách không đầy đủ.</li>
                    <li>Dòng confidence thấp luôn đi qua bàn review thay vì tự đoán giá.</li>
                    <li>Chỉ dùng benchmark có holdout riêng để tránh data leakage.</li>
                  </ol>
                  ${safeReportUrl ? `<div class="card-body" style="padding-top:0"><a class="button button-secondary button-small" href="${safeReportUrl}" target="_blank" rel="noopener noreferrer">${icon("external")}<span>Mở báo cáo chi tiết</span></a></div>` : ""}
                </section>
              </div>
              <section class="card" style="margin-top:18px"><div class="card-header"><div class="card-header-copy"><h2>Raw benchmark response</h2><p>Dùng để audit / debug khi điều chỉnh matcher.</p></div></div><div class="card-body"><pre style="margin:0;max-height:260px;overflow:auto;color:var(--ink-soft);font-size:11px;white-space:pre-wrap">${escapeHtml(JSON.stringify(state.benchmark, null, 2))}</pre></div></section>
            `
            : `<section class="card">${emptyState({ iconName: "layers", title: "Chưa có benchmark", description: "Chạy holdout benchmark ở backend để xem coverage thật của parser, material matcher và labor matcher.", actionLabel: "Về Import Center", action: "import" })}</section>`
      }
    `;
  }

  function coverageRow(label, value, className) {
    const numeric = value === null || value === undefined || value === "" ? null : Number(value) <= 1 ? Number(value) * 100 : Number(value);
    const width = numeric === null || Number.isNaN(numeric) ? 0 : Math.max(0, Math.min(100, numeric));
    return `<div class="coverage-row"><span class="coverage-row-label">${escapeHtml(label)}</span><span class="coverage-bar ${className}"><span style="width:${width}%"></span></span><span class="coverage-row-value">${value === null || value === undefined ? "—" : percentage(value)}</span></div>`;
  }

  async function exportQuotation(id) {
    try {
      const result = await api.exportQuotation(id);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("File Excel đã được tải xuống và có thể gửi tiếp cho khách hàng.", "success", "Xuất thành công");
    } catch (error) {
      toast(error.message, "error", "Không thể xuất Excel");
    }
  }

  function sourceDetailRecords(source, names) {
    const nested = source.raw?.catalog || source.raw?.records || source.raw?.data || {};
    const rawPayload = source.raw || {};
    const records = names.flatMap((name) => [
      ...rawArray(source, [name]),
      ...rawArray(rawPayload, [name]),
      ...rawArray(nested, [name]),
    ]);
    const seen = new Set();
    return records.filter((record) => {
      if (!record || typeof record !== "object") return true;
      const key = pick(record, ["id", "product_id", "labor_item_id", "source_row_id", "row_id"], "");
      if (key === "") return true;
      const signature = `${nameSignature(record)}:${key}`;
      if (seen.has(signature)) return false;
      seen.add(signature);
      return true;
    });
  }

  function nameSignature(record) {
    return pick(record, ["kind", "record_type", "observation_type", "type"], "record");
  }

  function sourcePointerFromRecord(record, source) {
    const pointerValue = record?.source || record?.provenance;
    const pointer = Array.isArray(pointerValue) ? pointerValue[0] : pointerValue;
    if (pointer && typeof pointer === "object" && Object.keys(pointer).length) return pointer;
    return {
      file_id: source.id || "",
      filename: source.filename || "",
      status: source.lifecycleStatus || source.status || "ACTIVE",
      sheet_id: pick(record, ["source_sheet_id", "sheet_id"], ""),
      sheet_name: pick(record, ["sheet_name", "source_sheet"], ""),
      row_id: pick(record, ["source_row_id", "row_id"], ""),
      row_no: pick(record, ["row_no", "source_row", "row_number"], ""),
    };
  }

  function legacyCatalogSource(source, record, kind) {
    const idKey = kind === "labor" ? "labor_item_id" : "product_id";
    const relatedNames = kind === "labor" ? ["labor_rates", "rates"] : ["prices", "price_observations", "observations"];
    const related = sourceDetailRecords(source, relatedNames).find(
      (candidate) => String(candidate?.[idKey] ?? "") === String(record?.id ?? ""),
    );
    return related ? sourcePointerFromRecord(related, source) : sourcePointerFromRecord(record, source);
  }

  function sourcePriceRecords(source) {
    const observations = sourceDetailRecords(source, ["price_observations", "observations"]);
    // New imports keep immutable observations as the source of truth while
    // product_prices remains a compatibility projection. Prefer observations
    // so one workbook row is not rendered twice in the source detail modal.
    if (observations.length) return observations;
    return sourceDetailRecords(source, ["prices", "price_records"]);
  }

  function renderSourceDetailModal(source, loading = false, errorMessage = "") {
    const lifecycleValue = source.lifecycleStatus || source.status;
    const lifecycle = sourceStatus(lifecycleValue);
    const sheets = source.sheetsData?.length ? source.sheetsData : rawArray(source.raw, ["sheets", "worksheets"]);
    const products = sourceDetailRecords(source, ["products", "product_records", "catalog_products"]);
    const prices = sourcePriceRecords(source);
    const labor = sourceDetailRecords(source, ["labor", "labor_items", "labor_records", "labor_rates"]);
    const boq = sourceDetailRecords(source, ["boq", "boq_items", "boq_records"]);
    const warnings = rawArray(source.raw, ["warnings", "parse_warnings"]).length
      ? rawArray(source.raw, ["warnings", "parse_warnings"])
      : rawArray(source.metadata, ["warnings", "parse_warnings"]);
    const summary = source.summary || {};
    const referenceCount = countValue(source.referencedCount, 0);
    const isArchived = ["archived", "inactive", "retired"].includes(String(lifecycleValue || "").toLowerCase());
    const isSuperseded = ["superseded", "outdated", "replaced"].includes(String(lifecycleValue || "").toLowerCase());
    const priceRecordCount = pick(
      summary,
      ["price_observations", "observation_count", "prices", "price_count"],
      prices.length,
    );
    modalRoot.innerHTML = `
      <div class="modal-backdrop" data-modal-dismiss>
        <section class="modal modal-wide" role="dialog" aria-modal="true" aria-labelledby="source-detail-title">
          <div class="modal-header">
            <div>
              <p class="eyebrow">WORKBOOK ĐÃ NHẬP</p>
              <h2 id="source-detail-title">${escapeHtml(source.filename)}</h2>
              <p class="modal-subtitle">${escapeHtml(sourceTypeLabel(source.confirmedType || source.detectedType))} • nhập ${formatDateTime(source.uploadedAt)}</p>
            </div>
            <button class="icon-button" data-modal-dismiss aria-label="Đóng">${icon("x")}</button>
          </div>
          <div class="modal-body source-detail-body">
            ${loading ? `<div class="card-body">${skeletonCard()}</div>` : ""}
            ${errorMessage ? `<div class="callout callout-warning">${icon("alert")}<span>${escapeHtml(errorMessage)}. Đang hiển thị dữ liệu đã tải trước đó.</span></div>` : ""}
            <div class="source-detail-toolbar">
              <span class="badge ${lifecycle.className}">${lifecycle.label}</span>
              ${source.lifecycleReason ? `<span class="cell-muted">${escapeHtml(source.lifecycleReason)}</span>` : ""}
              <span class="source-detail-toolbar-spacer"></span>
              ${
                isSuperseded
                  ? `<span class="cell-muted">Chỉ đọc — dùng phiên bản thay thế để báo giá mới.</span>`
                  : isArchived
                  ? `<button class="button button-secondary button-small" data-action="restore-source" data-id="${escapeHtml(source.id)}">${icon("refresh")}<span>Khôi phục sử dụng</span></button>`
                  : `<button class="button button-secondary button-small" data-action="archive-source" data-id="${escapeHtml(source.id)}">${icon("layers")}<span>Lưu trữ / đánh dấu cũ</span></button>`
              }
              <button class="button button-danger button-small" data-action="delete-source" data-id="${escapeHtml(source.id)}">${icon("x")}<span>Xóa an toàn</span></button>
            </div>
            <div class="callout ${referenceCount > 0 ? "callout-warning" : "callout-success"}">
              ${icon(referenceCount > 0 ? "alert" : "check-circle")}
              <span>${referenceCount > 0
                ? `Nguồn này đang được ${formatNumber(referenceCount)} record tham chiếu. Lưu trữ sẽ giữ nguyên lịch sử/provenance; xóa cứng có thể bị API từ chối.`
                : "Raw workbook, mapping và provenance được giữ lại để audit. Khi dữ liệu lỗi thời, nên lưu trữ thay vì xóa cứng."}</span>
            </div>
            ${
              source.supersededBy || source.raw?.lifecycle?.superseded_by_source_file_id
                ? `<div class="callout callout-warning">${icon("refresh")}<span>Workbook này đã có phiên bản thay thế (#${escapeHtml(source.supersededBy || source.raw?.lifecycle?.superseded_by_source_file_id)}). Các giá cũ chỉ còn dùng để tra cứu lịch sử.</span></div>`
                : ""
            }
            <div class="source-summary-grid">
              ${sourceSummaryTile("Sheet", pick(summary, ["sheets", "sheet_count"], source.sheets), "file")}
              ${sourceSummaryTile("Dòng nhận diện", pick(summary, ["rows", "row_count", "extracted_rows"], source.rows), "layers")}
              ${sourceSummaryTile("Vật tư / giá", `${formatNumber(pick(summary, ["products", "product_count"], products.length))} / ${formatNumber(priceRecordCount)}`, "database")}
              ${sourceSummaryTile("Nhân công / BOQ", `${formatNumber(pick(summary, ["labor", "labor_count"], labor.length))} / ${formatNumber(pick(summary, ["boq", "boq_count"], boq.length))}`, "book")}
            </div>
            <div class="source-detail-sections">
              <details open>
                <summary>Sheets, header và mapping <span class="badge badge-neutral">${formatNumber(sheets.length)}</span></summary>
                <div class="source-detail-section-content">
                  ${sheets.length ? renderSourceSheets(sheets) : `<p class="cell-muted">API chưa trả về chi tiết sheet cho workbook này.</p>`}
                  ${
                    source.rawRowsLoaded
                      ? renderSourceRawRows(sheets)
                      : `<div class="source-row-preview-cta"><span class="cell-muted">${products.length + prices.length + labor.length + boq.length ? "Có thể xem raw row khi cần đối chiếu parser." : "Workbook chưa tạo record dẫn xuất; xem raw row để kiểm tra dữ liệu parser đã đọc."}</span><button class="button button-secondary button-small" data-action="source-row-preview" data-id="${escapeHtml(source.id)}">${icon("search")}<span>Xem mẫu raw row</span></button></div>`
                  }
                </div>
              </details>
              <details open>
                <summary>Hạng mục danh mục phát sinh <span class="badge badge-neutral">${formatNumber(products.length + labor.length)}</span></summary>
                <div class="source-detail-section-content">
                  ${renderSourceRecordGroups(source, products, labor)}
                </div>
              </details>
              <details>
                <summary>Quan sát giá / lịch sử giá <span class="badge badge-neutral">${formatNumber(priceRecordCount)}</span></summary>
                <div class="source-detail-section-content">
                  ${prices.length ? renderSourcePriceRecords(prices, source) : `<p class="cell-muted">Không có price observation trả về từ API.</p>`}
                </div>
              </details>
              <details>
                <summary>Dòng BOQ đã nhận <span class="badge badge-neutral">${formatNumber(boq.length)}</span></summary>
                <div class="source-detail-section-content">
                  ${boq.length ? renderSourceBoqRecords(boq) : `<p class="cell-muted">Không có dòng BOQ thuộc workbook này.</p>`}
                </div>
              </details>
              <details ${warnings.length ? "open" : ""}>
                <summary>Cảnh báo parse / dữ liệu <span class="badge ${warnings.length ? "badge-warning" : "badge-success"}">${formatNumber(warnings.length)}</span></summary>
                <div class="source-detail-section-content">
                  ${warnings.length ? `<ul class="warning-list">${warnings.map((warning) => `<li>${icon("alert")}<span>${escapeHtml(typeof warning === "string" ? warning : pick(warning, ["message", "warning", "detail"], JSON.stringify(warning)))}</span></li>`).join("")}</ul>` : `<p class="cell-muted">Không có cảnh báo được ghi nhận.</p>`}
                </div>
              </details>
            </div>
            <details class="source-raw-details">
              <summary>Metadata kỹ thuật (audit)</summary>
              <pre>${escapeHtml(JSON.stringify(source.metadata || source.raw || {}, null, 2))}</pre>
            </details>
          </div>
          <div class="modal-footer">
            <span class="cell-muted source-modal-footer-note">ID nguồn: ${escapeHtml(source.id || "—")}</span>
            <button class="button button-secondary" data-modal-dismiss>Đóng</button>
          </div>
        </section>
      </div>
    `;
    bindModalDismiss(() => {});
  }

  function sourceSummaryTile(label, value, iconName) {
    return `<div class="source-summary-tile"><span class="source-summary-icon">${icon(iconName)}</span><span><strong>${typeof value === "string" ? escapeHtml(value) : formatNumber(value)}</strong><span>${escapeHtml(label)}</span></span></div>`;
  }

  function renderSourceSheets(sheets) {
    return `
      <div class="table-wrap">
        <table class="data-table compact-table">
          <thead><tr><th>Sheet</th><th>Loại nhận diện</th><th>Header</th><th>Mapping</th><th>Dòng</th><th>Cảnh báo</th></tr></thead>
          <tbody>
            ${sheets.map((sheet) => {
              const mapping = sheet.mapping || sheet.header_mapping || sheet.mapping_json || {};
              const warnings = rawArray(sheet, ["warnings", "parse_warnings"]);
              return `<tr>
                <td><span class="cell-strong">${escapeHtml(pick(sheet, ["sheet_name", "name"], "Sheet"))}</span><div class="cell-muted">#${formatNumber(pick(sheet, ["sheet_index", "index"], ""))}</div></td>
                <td><span class="badge badge-neutral">${escapeHtml(sourceTypeLabel(pick(sheet, ["detected_type", "type"], "unknown")))}</span></td>
                <td class="cell-number">${formatNumber(pick(sheet, ["header_row", "header"], "—"))}</td>
                <td><pre class="inline-json">${escapeHtml(JSON.stringify(mapping))}</pre></td>
                <td class="cell-number">${formatNumber(pick(sheet, ["data_row_count", "data_rows", "row_count", "rows"], 0))}</td>
                <td>${warnings.length ? `<span class="badge badge-warning">${formatNumber(warnings.length)}</span>` : `<span class="badge badge-success">0</span>`}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
    `;
  }

  function renderSourceRawRows(sheets) {
    const sheetsWithRows = sheets.filter((sheet) => rawArray(sheet, ["rows"]).length);
    if (!sheetsWithRows.length) {
      return `<div class="callout callout-warning" style="margin-top:10px">${icon("info")}<span>API đã tải chi tiết nhưng workbook không có raw row được lưu cho các sheet này.</span></div>`;
    }
    return `
      <div class="source-raw-row-preview">
        <h3 class="source-record-heading">Raw row preview</h3>
        ${sheetsWithRows.map((sheet) => {
          const rows = rawArray(sheet, ["rows"]);
          const visibleRows = rows.slice(0, 25);
          return `
            <details class="source-row-sheet" open>
              <summary>${escapeHtml(pick(sheet, ["sheet_name", "name"], "Sheet"))}<span class="badge badge-neutral">${formatNumber(rows.length)} row đã lưu</span></summary>
              <div class="source-detail-section-content">
                <div class="table-wrap"><table class="data-table compact-table source-raw-row-table">
                  <thead><tr><th>#</th><th>Loại dòng</th><th>Raw cells</th><th>Cảnh báo</th></tr></thead>
                  <tbody>${visibleRows.map((row) => {
                    const warnings = rawArray(row, ["warnings", "parse_warnings"]);
                    const rawCells = row.raw_cells || row.cells || row.raw || {};
                    return `<tr><td class="cell-number">${escapeHtml(pick(row, ["row_no", "row_number"], "—"))}</td><td><span class="badge badge-neutral">${escapeHtml(pick(row, ["row_kind", "kind"], "unknown"))}</span></td><td><pre class="inline-json source-row-json">${escapeHtml(JSON.stringify(rawCells))}</pre></td><td>${warnings.length ? `<span class="badge badge-warning">${formatNumber(warnings.length)}</span>` : `<span class="badge badge-success">0</span>`}</td></tr>`;
                  }).join("")}</tbody>
                </table></div>
                ${rows.length > visibleRows.length ? `<p class="cell-muted">Đang hiển thị ${formatNumber(visibleRows.length)} row đầu tiên trong ${formatNumber(rows.length)} row đã tải.</p>` : ""}
              </div>
            </details>
          `;
        }).join("")}
      </div>
    `;
  }

  function renderSourceRecordGroups(source, products, labor) {
    const productRows = products.slice(0, 80).map((record) => {
      const item = normalizeCatalogItem(
        record?.source || (Array.isArray(record?.provenance) && record.provenance.length)
          ? record
          : { ...record, source: legacyCatalogSource(source, record, "product") },
        "product",
      );
      return `<tr><td><button class="button button-quiet catalog-item-link" data-action="catalog-detail" data-kind="product" data-id="${escapeHtml(item.id)}">${escapeHtml(item.name)}</button>${item.code ? `<div class="cell-muted">${escapeHtml(item.code)}</div>` : ""}</td><td>${escapeHtml(item.category)}</td><td>${escapeHtml(item.unit || "—")}</td><td class="cell-muted">${escapeHtml(item.sourceSheet || "—")} ${item.sourceRow ? `• dòng ${escapeHtml(item.sourceRow)}` : ""}</td></tr>`;
    });
    const laborRows = labor.slice(0, 80).map((record) => {
      const item = normalizeCatalogItem(
        record?.source || (Array.isArray(record?.provenance) && record.provenance.length)
          ? record
          : { ...record, source: legacyCatalogSource(source, record, "labor") },
        "labor",
      );
      return `<tr><td><button class="button button-quiet catalog-item-link" data-action="catalog-detail" data-kind="labor" data-id="${escapeHtml(item.id)}">${escapeHtml(item.name)}</button>${item.code ? `<div class="cell-muted">${escapeHtml(item.code)}</div>` : ""}</td><td>${escapeHtml(item.category)}</td><td>${escapeHtml(item.unit || "—")}</td><td class="cell-muted">${escapeHtml(item.sourceSheet || "—")} ${item.sourceRow ? `• dòng ${escapeHtml(item.sourceRow)}` : ""}</td></tr>`;
    });
    if (!productRows.length && !laborRows.length) return `<p class="cell-muted">API chưa trả về product/labor records riêng cho workbook này. Hãy xem raw metadata hoặc làm mới sau khi ingest hoàn tất.</p>`;
    return `
      ${productRows.length ? `<h3 class="source-record-heading">Vật tư (${formatNumber(products.length)})</h3><div class="table-wrap"><table class="data-table compact-table"><thead><tr><th>Tên / mã</th><th>Phân loại</th><th>Đơn vị</th><th>Provenance</th></tr></thead><tbody>${productRows.join("")}</tbody></table></div>` : ""}
      ${laborRows.length ? `<h3 class="source-record-heading">Nhân công (${formatNumber(labor.length)})</h3><div class="table-wrap"><table class="data-table compact-table"><thead><tr><th>Tên / mã</th><th>Phân loại</th><th>Đơn vị</th><th>Provenance</th></tr></thead><tbody>${laborRows.join("")}</tbody></table></div>` : ""}
      ${(products.length + labor.length) > 160 ? `<p class="cell-muted">Đang hiển thị 160 record đầu tiên. Dùng bộ lọc ở Danh mục & giá để xem toàn bộ.</p>` : ""}
    `;
  }

  function renderSourcePriceRecords(prices, source) {
    return `<div class="table-wrap"><table class="data-table compact-table"><thead><tr><th>Sản phẩm</th><th>Loại dữ liệu</th><th>Nhà cung cấp</th><th>Giá net</th><th>Thuế</th><th>Hiệu lực</th><th>Provenance</th></tr></thead><tbody>${prices.slice(0, 120).map((record) => {
      const item = normalizeCatalogItem(
        record?.source || (Array.isArray(record?.provenance) && record.provenance.length)
          ? record
          : { ...record, source: sourcePointerFromRecord(record, source) },
        "price",
      );
      const productId = pick(record, ["product_id"], item.id);
      const recordType = pick(
        record,
        ["observation_type", "record_type"],
        record.context_json !== undefined || record.context !== undefined ? "price_observation" : "operational_price",
      );
      return `<tr><td><button class="button button-quiet catalog-item-link" data-action="catalog-detail" data-kind="product" data-id="${escapeHtml(productId)}">${escapeHtml(item.name)}</button></td><td><span class="badge badge-neutral">${escapeHtml(recordType)}</span></td><td class="cell-muted">${escapeHtml(item.supplier || "—")}</td><td class="cell-number">${formatCurrency(item.currentPrice)}</td><td class="cell-muted">${escapeHtml(item.taxMode || "ex_vat")}</td><td class="cell-muted">${formatDate(item.effectiveDate)}</td><td class="cell-muted">${escapeHtml(item.sourceSheet || "—")} ${item.sourceRow ? `• dòng ${escapeHtml(item.sourceRow)}` : ""}</td></tr>`;
    }).join("")}</tbody></table></div>${prices.length > 120 ? `<p class="cell-muted">Đang hiển thị 120 quan sát đầu tiên.</p>` : ""}`;
  }

  function renderSourceBoqRecords(boq) {
    return `<div class="table-wrap"><table class="data-table compact-table"><thead><tr><th>Mô tả</th><th>Section</th><th>Đơn vị</th><th>Khối lượng</th><th>Trạng thái</th></tr></thead><tbody>${boq.slice(0, 120).map((record) => {
      const status = reviewStatus(pick(record, ["status", "review_status"], "review_required"));
      return `<tr><td class="cell-strong">${escapeHtml(pick(record, ["raw_description", "description", "name"], "Dòng BOQ"))}</td><td class="cell-muted">${escapeHtml(pick(record, ["section", "group"], "—"))}</td><td class="cell-muted">${escapeHtml(pick(record, ["unit", "uom"], "—"))}</td><td class="cell-number">${formatNumber(pick(record, ["quantity", "qty"], "—"))}</td><td><span class="badge ${status.className}">${status.label}</span></td></tr>`;
    }).join("")}</tbody></table></div>${boq.length > 120 ? `<p class="cell-muted">Đang hiển thị 120 dòng đầu tiên.</p>` : ""}`;
  }

  async function showSourceDetail(id) {
    const existing = state.sources.find((item) => String(item.id) === String(id));
    if (!existing) {
      toast("Không tìm thấy workbook trong danh sách hiện tại.", "warning", "Nguồn dữ liệu");
      return;
    }
    state.activeSource = existing;
    renderSourceDetailModal(existing, true);
    try {
      const payload = await api.source(id, { includeRows: false, limit: 500 });
      state.activeSource = normalizeSource(payload?.source || payload?.data || payload || existing);
      renderSourceDetailModal(state.activeSource);
    } catch (error) {
      renderSourceDetailModal(existing, false, error.message);
    }
  }

  async function previewSourceRows(id) {
    const source = state.activeSource && String(state.activeSource.id) === String(id)
      ? state.activeSource
      : state.sources.find((item) => String(item.id) === String(id));
    if (!source) return;
    renderSourceDetailModal(source, true);
    try {
      const payload = await api.source(id, { includeRows: true, limit: 300 });
      const detailed = normalizeSource(payload?.source || payload?.data || payload || source);
      detailed.rawRowsLoaded = true;
      state.activeSource = detailed;
      renderSourceDetailModal(detailed);
    } catch (error) {
      renderSourceDetailModal(source, false, error.message);
      toast(error.message, "warning", "Không tải được raw row preview");
    }
  }

  async function refreshSourceAfterMutation(id, message, title = "Đã cập nhật nguồn") {
    toast(message, "success", title);
    await loadSources();
    if (state.activeSource && String(state.activeSource.id) === String(id)) {
      const source = state.sources.find((item) => String(item.id) === String(id));
      if (source) {
        state.activeSource = source;
        renderSourceDetailModal(source, true);
        try {
          const payload = await api.source(id, { includeRows: false, limit: 500 });
          state.activeSource = normalizeSource(payload?.source || payload?.data || payload || source);
        } catch (_) {
          // List response is enough to keep the modal usable.
        }
        renderSourceDetailModal(state.activeSource);
      } else {
        modalRoot.innerHTML = "";
      }
    }
    if (state.route === "catalog") await loadCatalogItems();
  }

  async function archiveSource(id) {
    const source = state.sources.find((item) => String(item.id) === String(id));
    if (!source || !window.confirm(`Lưu trữ workbook "${source.filename}"?\n\nCác record và provenance vẫn được giữ, nhưng nguồn sẽ không còn được chọn làm giá hiện hành.`)) return;
    try {
      await api.archiveSource(id);
      await refreshSourceAfterMutation(id, "Workbook đã được lưu trữ; lịch sử và provenance vẫn nguyên vẹn.");
    } catch (error) {
      toast(error.message, "error", "Không thể lưu trữ workbook");
    }
  }

  async function restoreSource(id) {
    try {
      await api.restoreSource(id);
      await refreshSourceAfterMutation(id, "Workbook đã được khôi phục vào nguồn đang hoạt động.");
    } catch (error) {
      toast(error.message, "error", "Không thể khôi phục workbook");
    }
  }

  async function deleteSource(id) {
    const source = state.sources.find((item) => String(item.id) === String(id));
    if (!source || !window.confirm(`Xóa workbook "${source.filename}"?\n\nChỉ nên xóa nguồn chưa được tham chiếu. Nếu đã có provenance, máy chủ sẽ từ chối để bảo vệ lịch sử.`)) return;
    try {
      await api.deleteSource(id);
      toast("Workbook đã được xóa khỏi kho dữ liệu.", "success", "Đã xóa nguồn");
      modalRoot.innerHTML = "";
      await loadSources();
      if (state.route === "catalog") await loadCatalogItems();
    } catch (error) {
      toast(error.message, "warning", "Không thể xóa cứng workbook");
    }
  }

  function renderCatalogItemModal(item, loading = false, errorMessage = "") {
    const status = catalogStatus(item?.status);
    const observations = item?.observations || rawArray(item?.raw, ["observations", "prices", "price_observations", "history"]);
    const technical = item?.technicalAttributes || item?.raw?.technical_attributes || {};
    modalRoot.innerHTML = `
      <div class="modal-backdrop" data-modal-dismiss>
        <section class="modal modal-wide" role="dialog" aria-modal="true" aria-labelledby="catalog-detail-title">
          <div class="modal-header">
            <div>
              <p class="eyebrow">DANH MỤC & GIÁ</p>
              <h2 id="catalog-detail-title">${escapeHtml(item?.name || "Chi tiết hạng mục")}</h2>
              <p class="modal-subtitle">${escapeHtml(catalogKindLabel(item?.kind))}${item?.code ? ` • mã ${escapeHtml(item.code)}` : ""}</p>
            </div>
            <button class="icon-button" data-modal-dismiss aria-label="Đóng">${icon("x")}</button>
          </div>
          <div class="modal-body catalog-detail-body">
            ${loading ? `<div class="card-body">${skeletonCard()}</div>` : ""}
            ${errorMessage ? `<div class="callout callout-warning">${icon("alert")}<span>${escapeHtml(errorMessage)}. Đang hiển thị bản ghi trong danh sách.</span></div>` : ""}
            ${
              item
                ? `
                  <div class="source-detail-toolbar">
                    <span class="badge ${status.className}">${status.label}</span>
                    <span class="cell-muted">${item.category ? escapeHtml(item.category) : "Chưa phân loại"}${item.unit ? ` • ${escapeHtml(item.unit)}` : ""}</span>
                    <span class="source-detail-toolbar-spacer"></span>
                    ${
                      ["archived", "inactive", "retired"].includes(String(item.status || "").toLowerCase())
                        ? `<button class="button button-secondary button-small" data-action="restore-catalog-item" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind)}">${icon("refresh")}<span>Khôi phục hạng mục</span></button>`
                        : `<button class="button button-secondary button-small" data-action="archive-catalog-item" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind)}">${icon("layers")}<span>Lưu trữ hạng mục</span></button>`
                    }
                    <button class="button button-danger button-small" data-action="delete-catalog-item" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind)}">${icon("x")}<span>Xóa an toàn</span></button>
                  </div>
                  <div class="source-summary-grid">
                    ${sourceSummaryTile(item.kind === "labor" ? "Đơn giá nhân công" : "Giá net hiện hành", formatCurrency(item.currentPrice), "layers")}
                    ${sourceSummaryTile("Quan sát / phiên bản", item.observationCount, "calendar")}
                    ${sourceSummaryTile("Nguồn", item.sourceFilename || (item.sourceId ? `#${item.sourceId}` : "Thiếu"), "file")}
                    ${sourceSummaryTile("Hiệu lực", item.effectiveDate ? formatDate(item.effectiveDate) : "Chưa có", "calendar")}
                  </div>
                  <div class="catalog-detail-grid">
                    <section class="detail-panel">
                      <h3>Thông tin chuẩn hóa</h3>
                      <dl class="quotation-summary">
                        <div class="summary-row"><dt>Tên chuẩn hóa</dt><dd>${escapeHtml(item.name)}</dd></div>
                        <div class="summary-row"><dt>Mã</dt><dd>${escapeHtml(item.code || "—")}</dd></div>
                        <div class="summary-row"><dt>Nhóm / phân loại</dt><dd>${escapeHtml(item.category || "Chưa phân loại")}${item.subcategory ? ` • ${escapeHtml(item.subcategory)}` : ""}</dd></div>
                        <div class="summary-row"><dt>Hãng / xuất xứ</dt><dd>${escapeHtml([item.brand, item.origin].filter(Boolean).join(" • ") || "—")}</dd></div>
                        <div class="summary-row"><dt>Thuế / tiền tệ</dt><dd>${escapeHtml(item.taxMode || "ex_vat")} • ${escapeHtml(item.currency || "VND")}</dd></div>
                      </dl>
                    </section>
                    <section class="detail-panel">
                      <h3>Provenance</h3>
                      ${
                        item.sourceId
                          ? `<button class="source-provenance-card" data-action="source-detail" data-id="${escapeHtml(item.sourceId)}"><span class="source-provenance-icon">${icon("file")}</span><span><strong>${escapeHtml(catalogSourceLabel(item))}</strong><span>${escapeHtml(item.sourceSheet || "Sheet chưa rõ")}${item.sourceRow ? ` • dòng ${escapeHtml(item.sourceRow)}` : ""}</span></span>${icon("chevron-right")}</button>`
                          : `<div class="callout callout-warning">${icon("alert")}<span>Record này chưa có workbook/sheet/dòng nguồn. Không nên áp dụng tự động cho báo giá.</span></div>`
                      }
                    </section>
                  </div>
                  <details class="source-raw-details" open>
                    <summary>Lịch sử giá / observation (${formatNumber(observations.length)})</summary>
                    <div class="source-detail-section-content">${observations.length ? renderCatalogObservations(observations) : `<p class="cell-muted">Chưa có observation chi tiết.</p>`}</div>
                  </details>
                  <details class="source-raw-details">
                    <summary>Thuộc tính kỹ thuật / alias</summary>
                    <pre>${escapeHtml(JSON.stringify({ technical_attributes: technical, aliases: item.aliases || [] }, null, 2))}</pre>
                  </details>
                `
                : `<div class="callout callout-warning">${icon("alert")}<span>Không có dữ liệu hạng mục.</span></div>`
            }
          </div>
          <div class="modal-footer"><span class="cell-muted source-modal-footer-note">ID record: ${escapeHtml(item?.id || "—")}</span><button class="button button-secondary" data-modal-dismiss>Đóng</button></div>
        </section>
      </div>
    `;
    bindModalDismiss(() => {});
  }

  function renderCatalogObservations(observations) {
    return `<div class="table-wrap"><table class="data-table compact-table"><thead><tr><th>Giá net</th><th>Nhà cung cấp</th><th>Loại quan sát</th><th>Thuế</th><th>Hiệu lực</th><th>Nguồn</th></tr></thead><tbody>${observations.slice(0, 160).map((observation) => {
      const price = pick(observation, ["net_price", "price", "rate", "value"], null);
      return `<tr><td class="cell-number">${formatCurrency(price)}</td><td class="cell-muted">${escapeHtml(pick(observation, ["supplier", "vendor"], "—"))}</td><td><span class="badge badge-neutral">${escapeHtml(pick(observation, ["observation_type", "source_type", "type"], "supplier"))}</span></td><td class="cell-muted">${escapeHtml(pick(observation, ["tax_mode", "tax_basis"], "ex_vat"))}</td><td class="cell-muted">${formatDate(pick(observation, ["effective_date", "created_at"], ""))}</td><td class="cell-muted">${escapeHtml(pick(observation, ["source_filename", "filename", "sheet_name"], "—"))}</td></tr>`;
    }).join("")}</tbody></table></div>${observations.length > 160 ? `<p class="cell-muted">Đang hiển thị 160 observation đầu tiên.</p>` : ""}`;
  }

  async function showCatalogDetail(kind, id) {
    const existing = state.catalogItems.find((item) => String(item.id) === String(id) && (item.kind === kind || kind === "price"));
    if (!existing) {
      toast("Không tìm thấy record trong danh sách hiện tại.", "warning", "Danh mục");
      return;
    }
    state.activeCatalogItem = existing;
    renderCatalogItemModal(existing, true);
    try {
      const payload = await api.catalogItem(kind, id);
      state.activeCatalogItem = normalizeCatalogItem(payload?.item || payload?.data || payload || existing, kind);
      renderCatalogItemModal(state.activeCatalogItem);
    } catch (error) {
      renderCatalogItemModal(existing, false, error.message);
    }
  }

  async function archiveCatalogItem(kind, id) {
    const item = state.catalogItems.find((candidate) => String(candidate.id) === String(id) && candidate.kind === kind);
    if (!item || !window.confirm(`Lưu trữ hạng mục "${item.name}"?\n\nGiá và provenance cũ vẫn giữ trong lịch sử, nhưng record sẽ không còn là lựa chọn mặc định.`)) return;
    try {
      await api.archiveCatalogItem(item);
      toast("Hạng mục đã được lưu trữ.", "success", "Đã cập nhật danh mục");
      modalRoot.innerHTML = "";
      await loadCatalogItems();
    } catch (error) {
      toast(error.message, "warning", "Không thể lưu trữ hạng mục");
    }
  }

  async function restoreCatalogItem(kind, id) {
    const item = state.catalogItems.find((candidate) => String(candidate.id) === String(id) && candidate.kind === kind);
    if (!item) return;
    try {
      await api.restoreCatalogItem(item);
      toast("Hạng mục đã được khôi phục.", "success", "Đã cập nhật danh mục");
      modalRoot.innerHTML = "";
      await loadCatalogItems();
    } catch (error) {
      toast(error.message, "warning", "Không thể khôi phục hạng mục");
    }
  }

  async function deleteCatalogItem(kind, id) {
    const item = state.catalogItems.find((candidate) => String(candidate.id) === String(id) && candidate.kind === kind);
    if (!item || !window.confirm(`Xóa hạng mục "${item.name}"?\n\nChỉ record chưa được tham chiếu mới có thể xóa cứng. Nếu đang dùng trong báo giá, API sẽ bảo vệ provenance.`)) return;
    try {
      await api.deleteCatalogItem(item);
      toast("Hạng mục đã được xóa.", "success", "Đã xóa record");
      modalRoot.innerHTML = "";
      await loadCatalogItems();
    } catch (error) {
      toast(error.message, "warning", "Không thể xóa cứng hạng mục");
    }
  }

  function showGuide() {
    const aiDescription = describeAIStatus({ ai: state.aiStatus || {} });
    modalRoot.innerHTML = `
      <div class="modal-backdrop" data-modal-dismiss>
        <section class="modal" role="dialog" aria-modal="true" aria-labelledby="guide-title">
          <div class="modal-header"><div><h2 id="guide-title">Hướng dẫn nhanh</h2><p style="margin:4px 0 0;color:var(--ink-muted);font-size:11px">Ba bước để có một báo giá có nguồn.</p></div><button class="icon-button" data-modal-dismiss aria-label="Đóng">${icon("x")}</button></div>
          <div class="modal-body">
            <ol class="prose-list" style="padding-top:0">
              <li><strong>Nhập dữ liệu:</strong> đưa bảng giá NCC, nhân công và báo giá lịch sử vào Kho dữ liệu.</li>
              <li><strong>Tạo báo giá:</strong> upload BOQ mới; engine đọc workbook, match vật tư và áp giá deterministic.</li>
              <li><strong>Rà soát:</strong> xử lý dòng ambiguity, lưu correction, rồi xuất Excel có sheet AI Audit.</li>
            </ol>
            <div class="callout ${aiDescription.status === "online" ? "callout-success" : "callout-warning"}" style="margin-bottom:12px">${icon("spark")}<span><strong>${escapeHtml(aiDescription.label)}.</strong> AI chỉ hỗ trợ phân loại / rerank candidate đã có khi dòng mơ hồ; giá, khối lượng và provenance luôn do pipeline deterministic lấy từ database.</span></div>
            <div class="callout callout-warning">${icon("alert")}<span>Nếu không có provenance, hệ thống không tự sinh giá. Dòng đó sẽ được đánh dấu cần review hoặc chưa có giá.</span></div>
          </div>
          <div class="modal-footer"><button class="button button-primary" data-modal-dismiss>Đã hiểu</button></div>
        </section>
      </div>
    `;
    bindModalDismiss(() => {});
  }

  async function renderRoute() {
    const route = routeFromHash();
    state.route = route.base;
    setBreadcrumb(route.base);
    if (route.base === "dashboard") {
      renderDashboard();
      await loadDashboard();
    } else if (route.base === "import") {
      renderImport();
      await loadSources();
    } else if (route.base === "new") {
      state.newQuotationFiles = [];
      renderNewQuotation();
    } else if (route.base === "quotations") {
      renderQuotations();
      await loadQuotations();
    } else if (route.base === "quotation") {
      state.activeQuotation = null;
      renderQuotationDetail();
      await loadQuotation(route.id);
    } else if (route.base === "review") {
      await openReview(route.id);
    } else if (route.base === "catalog") {
      renderCatalog();
      await loadCatalog();
    } else if (route.base === "benchmark") {
      renderBenchmark();
      await loadBenchmark();
    }
    $$(".nav-item[data-route]").forEach((item) => item.classList.toggle("is-active", item.dataset.route === route.base || (route.base === "quotation" && item.dataset.route === "quotations") || route.base === "review" && item.dataset.route === "quotations"));
    pageView.focus({ preventScroll: true });
  }

  function filterQuotations() {
    const query = ($("#quotation-search")?.value || "").trim().toLowerCase();
    const filter = $("#quotation-status-filter")?.value || "all";
    const items = state.quotations.filter((item) => {
      const haystack = `${item.projectName} ${item.customer} ${item.sourceFilename}`.toLowerCase();
      const status = String(item.status).toLowerCase();
      const matchesFilter =
        filter === "all" ||
        (filter === "completed" && ["completed", "complete", "priced", "done", "exported"].includes(status)) ||
        (filter === "review" && ["review", "review_required", "needs_review", "price_drift_warning", "external_quotation_required", "needs_supplier_quotation"].includes(status)) ||
        (filter === "running" && ["running", "processing", "queued"].includes(status)) ||
        (filter === "draft" && ["draft", "new"].includes(status));
      return matchesFilter && (!query || haystack.includes(query));
    });
    const list = $("#quotation-list");
    if (list) list.innerHTML = renderQuotationTable(items);
  }

  document.addEventListener("click", async (event) => {
    const eventTarget = event.target instanceof Element
      ? event.target
      : event.target?.parentElement;
    const routeElement = eventTarget?.closest("[data-route]");
    if (routeElement) {
      const route = routeElement.dataset.route;
      if (route) navigate(`#${route}`);
      return;
    }
    const actionElement = eventTarget?.closest("[data-action]");
    if (!actionElement) return;
    const action = actionElement.dataset.action;
    const id = actionElement.dataset.id;
    if (action === "new-quotation") navigate("#new");
    else if (action === "import") navigate("#import");
    else if (action === "catalog") navigate("#catalog");
    else if (action === "benchmark") navigate("#benchmark");
    else if (action === "view-quotations") navigate("#quotations");
    else if (action === "open-quotation" && id) navigate(`#quotation/${encodeURIComponent(id)}`);
    else if (action === "review-quotation" && id) navigate(`#review/${encodeURIComponent(id)}`);
    else if (action === "refresh-sources") await loadSources();
    else if (action === "refresh-quotations") await loadQuotations();
    else if (action === "refresh-catalog") await loadCatalog();
    else if (action === "refresh-benchmark") await loadBenchmark();
    else if (action === "focus-import-input") {
      if (state.route !== "import") navigate("#import");
      else window.setTimeout(() => $("#import-file-input")?.click(), 0);
    } else if (action === "clear-import-files") {
      state.currentImportFiles = [];
      state.currentImportResults = [];
      renderImport();
    } else if (action === "remove-import-file") {
      const index = Number(actionElement.dataset.index);
      const removed = state.currentImportFiles.splice(index, 1)[0];
      if (removed) {
        state.currentImportResults = state.currentImportResults.filter((item) => item.filename !== removed.name);
      }
      renderImport();
    } else if (action === "submit-import") await submitImport();
    else if (action === "remove-quotation-file") {
      state.newQuotationFiles.splice(Number(actionElement.dataset.index), 1);
      renderNewQuotation();
    } else if (action === "cancel-new") navigate("#dashboard");
    else if (action === "rerun-quotation") await runQuotation(id);
    else if (action === "export-quotation") await exportQuotation(id);
    else if (action === "select-review") {
      state.selectedReviewId = id;
      renderReview();
    } else if (action === "approve-review") await reviewAction("approve", id);
    else if (action === "choose-candidate") await reviewAction("choose_candidate", id);
    else if (action === "manual-price") await reviewAction("manual_price", id);
    else if (action === "supplier-quote") await reviewAction("supplier_quote", id);
    else if (action === "ignore-review") await reviewAction("ignore", id);
    else if (action === "clear-review-filter") {
      state.reviewFilter = "all";
      state.reviewQuery = "";
      renderReview();
    } else if (action === "source-detail") await showSourceDetail(id);
    else if (action === "source-row-preview") await previewSourceRows(id);
    else if (action === "archive-source") await archiveSource(id);
    else if (action === "restore-source") await restoreSource(id);
    else if (action === "delete-source") await deleteSource(id);
    else if (action === "catalog-detail") await showCatalogDetail(actionElement.dataset.kind || "product", id);
    else if (action === "archive-catalog-item") await archiveCatalogItem(actionElement.dataset.kind || "product", id);
    else if (action === "restore-catalog-item") await restoreCatalogItem(actionElement.dataset.kind || "product", id);
    else if (action === "delete-catalog-item") await deleteCatalogItem(actionElement.dataset.kind || "product", id);
    else if (action === "catalog-page") {
      const direction = actionElement.dataset.direction;
      const totalPages = Math.max(1, Math.ceil((Number(state.catalogTotal) || 0) / state.catalogPageSize));
      const nextPage = direction === "next" ? state.catalogPage + 1 : state.catalogPage - 1;
      state.catalogPage = Math.max(0, Math.min(totalPages - 1, nextPage));
      await loadCatalogItems();
      pageView.focus({ preventScroll: true });
    }
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-action='classification-change']")) {
      const index = Number(event.target.dataset.index);
      const result = state.currentImportResults[index];
      if (result) result.confirmed_type = event.target.value;
    } else if (event.target.matches("#quotation-status-filter")) {
      filterQuotations();
    } else if (event.target.matches("#catalog-kind-filter, #catalog-category-filter, #catalog-source-filter, #catalog-status-filter")) {
      state.catalogPage = 0;
      if (event.target.matches("#catalog-kind-filter")) state.catalogFilters.kind = event.target.value;
      if (event.target.matches("#catalog-category-filter")) state.catalogFilters.category = event.target.value;
      if (event.target.matches("#catalog-source-filter")) state.catalogFilters.sourceId = event.target.value;
      if (event.target.matches("#catalog-status-filter")) state.catalogFilters.status = event.target.value;
      loadCatalogItems();
    } else if (event.target.matches("#review-filter")) {
      state.reviewFilter = event.target.value;
      renderReview();
    }
  });

  document.addEventListener("input", (event) => {
    if (event.target.matches("#quotation-search")) filterQuotations();
    else if (event.target.matches("#catalog-search")) {
      state.catalogFilters.query = event.target.value;
      state.catalogPage = 0;
      if (state.catalogSearchTimer) window.clearTimeout(state.catalogSearchTimer);
      state.catalogSearchTimer = window.setTimeout(() => loadCatalogItems(), 260);
    }
    else if (event.target.matches("#review-search")) {
      state.reviewQuery = event.target.value;
      const selected = state.selectedReviewId;
      renderReview();
      if (selected) {
        const input = $("#review-search");
        if (input) {
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        }
      }
    }
  });

  document.addEventListener("submit", async (event) => {
    if (event.target.matches("#new-quotation-form")) {
      event.preventDefault();
      await submitQuotation(event.target);
    }
  });

  $("#menu-toggle")?.addEventListener("click", openSidebar);
  $("#sidebar-close")?.addEventListener("click", closeSidebar);
  $("#sidebar-backdrop")?.addEventListener("click", closeSidebar);
  $("#open-guide")?.addEventListener("click", showGuide);
  $("#global-refresh")?.addEventListener("click", () => renderRoute());
  window.addEventListener("hashchange", renderRoute);

  loadAIHealth();
  renderRoute();
})();
