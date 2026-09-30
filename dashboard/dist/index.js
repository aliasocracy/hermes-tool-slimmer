(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useEffect, useMemo, useState } = SDK.hooks;
  const { Badge, Button, Card, CardContent, CardHeader, CardTitle } = SDK.components;

  function fmtNumber(value) {
    const number = Number(value || 0);
    return new Intl.NumberFormat().format(Number.isFinite(number) ? Math.round(number) : 0);
  }

  function fmtPercent(value) {
    const number = Number(value || 0);
    return (Number.isFinite(number) ? number : 0).toFixed(1).replace(/\.0$/, "") + "%";
  }

  function fmtMs(value) {
    const number = Number(value || 0);
    return (Number.isFinite(number) ? number : 0).toFixed(2) + " ms";
  }

  function fmtTime(timestamp) {
    if (!timestamp) return "No selections recorded";
    return new Date(Number(timestamp) * 1000).toLocaleString();
  }

  function fmtIndexTime(timestamp) {
    if (!timestamp) return "Never rebuilt";
    return new Date(Number(timestamp) * 1000).toLocaleString();
  }

  function shortChecksum(value) {
    return value ? String(value).slice(0, 12) : "none";
  }

  function Metric({ label, value, detail }) {
    return React.createElement("div", { className: "tool-slimmer-metric" },
      React.createElement("div", { className: "tool-slimmer-metric-label" }, label),
      React.createElement("div", { className: "tool-slimmer-metric-value" }, value),
      detail && React.createElement("div", { className: "tool-slimmer-muted text-xs" }, detail),
    );
  }

  function ToolPills({ tools, limit }) {
    const max = limit || 7;
    const shown = (tools || []).slice(0, max);
    return React.createElement("div", { className: "tool-slimmer-tools" },
      shown.map(function (tool) {
        return React.createElement("span", { key: tool, className: "tool-slimmer-pill" }, tool);
      }),
      (tools || []).length > shown.length &&
        React.createElement("span", { className: "tool-slimmer-pill" }, "+" + String(tools.length - shown.length)),
    );
  }

  function CheckRows({ rows }) {
    return React.createElement("div", { className: "tool-slimmer-checks" },
      rows.length === 0 && React.createElement("div", { className: "tool-slimmer-muted text-sm" }, "No checks available"),
      rows.map(function (row) {
        return React.createElement("div", { key: row.id || row.label, className: "tool-slimmer-check-row" },
          React.createElement("div", null,
            React.createElement("div", { className: "font-medium" }, row.label || String(row.id || "").replaceAll("_", " ")),
            row.message && React.createElement("div", { className: "tool-slimmer-muted text-xs" }, row.message),
          ),
          React.createElement(Badge, { variant: row.status === "pass" ? "default" : "outline" }, row.status || "info"),
        );
      }),
    );
  }

  function useToolSlimmerData() {
    const [state, setState] = useState({ loading: true, error: null, status: null, summary: null, indexInfo: null, advisor: null, privacy: null });

    function load() {
      setState(function (prev) { return Object.assign({}, prev, { loading: true, error: null }); });
      Promise.all([
        SDK.fetchJSON("/api/plugins/tool-slimmer/status"),
        SDK.fetchJSON("/api/plugins/tool-slimmer/summary?limit=1000"),
        SDK.fetchJSON("/api/plugins/tool-slimmer/index"),
        SDK.fetchJSON("/api/plugins/tool-slimmer/advisor?limit=1000"),
        SDK.fetchJSON("/api/plugins/tool-slimmer/privacy"),
      ]).then(function (results) {
        setState({
          loading: false,
          error: null,
          status: results[0],
          summary: results[1].summary,
          indexInfo: results[2].index,
          advisor: results[3].advisor,
          privacy: results[4].privacy,
        });
      }).catch(function (error) {
        setState(function (prev) {
          return Object.assign({}, prev, { loading: false, error: error && error.message ? error.message : "LOAD_FAILED" });
        });
      });
    }

    useEffect(function () {
      load();
    }, []);

    return Object.assign({}, state, { reload: load });
  }

  function ToolSlimmerPage() {
    const data = useToolSlimmerData();
    const [indexBusy, setIndexBusy] = useState(false);
    const [indexMessage, setIndexMessage] = useState(null);
    const [advisorBusy, setAdvisorBusy] = useState(false);
    const [advisorMessage, setAdvisorMessage] = useState(null);
    const [evalBusy, setEvalBusy] = useState(false);
    const [evalReport, setEvalReport] = useState(null);
    const [evalError, setEvalError] = useState(null);
    const [tuneTool, setTuneTool] = useState("");
    const summary = data.summary || {};
    const totals = summary.totals || {};
    const averages = summary.averages || {};
    const config = (data.status && data.status.config) || {};
    const statusIndex = (data.status && data.status.index) || {};
    const index = data.indexInfo || statusIndex || {};
    const doctor = (data.status && data.status.doctor && data.status.doctor.checks) || {};
    const recent = summary.recent || [];
    const indexDocs = index.documents || [];
    const indexSource = index.source_context || {};
    const liveSnapshots = index.live_snapshots || [];
    const uniqueLiveSnapshots = liveSnapshots.filter(function (snapshot, snapshotIndex) {
      const platform = snapshot.platform || snapshot.label || "";
      return liveSnapshots.findIndex(function (candidate) {
        return (candidate.platform || candidate.label || "") === platform;
      }) === snapshotIndex;
    });
    const advisor = data.advisor || {};
    const privacy = data.privacy || {};
    const recommendations = advisor.recommendations || [];
    const setupChecklist = advisor.setup_checklist || [];
    const latestDecision = recent.length ? recent[recent.length - 1] : null;
    const latestMetrics = latestDecision && latestDecision.metrics ? latestDecision.metrics : {};
    const latestSelected = latestMetrics.selected || [];
    const tuneTools = latestSelected.slice(0, 50);
    const latestCandidates = latestMetrics.top_candidates || [];
    const twoPass = config.two_pass || {};
    const recentTwoPass = recent.filter(function (event) {
      const metrics = event && event.metrics ? event.metrics : {};
      return metrics.mode === "two_pass" || Boolean(metrics.two_pass_phase || metrics.two_pass_fallback);
    });
    const latestTwoPass = recentTwoPass.length ? recentTwoPass[recentTwoPass.length - 1].metrics || {} : {};
    const twoPassFallbacks = recentTwoPass.filter(function (event) {
      return Boolean(event.metrics && event.metrics.two_pass_fallback);
    }).length;
    const twoPassMisses = recentTwoPass.filter(function (event) {
      const metrics = event.metrics || {};
      const requested = metrics.two_pass_requested_tools || [];
      const hydrated = metrics.two_pass_hydrated_tools || [];
      return requested.length > hydrated.length;
    }).length;
    const tuneProfile = latestDecision && latestDecision.context && latestDecision.context.platform
      ? latestDecision.context.platform
      : "default";
    const selectedTuneTool = tuneTool || tuneTools[0] || "";

    const topTools = useMemo(function () {
      return Object.entries(summary.top_selected_tools || {}).slice(0, 10);
    }, [summary.top_selected_tools]);

    const doctorRows = Object.entries(doctor).map(function ([name, check]) {
      return { id: name, label: name.replaceAll("_", " "), status: check.status, message: check.message };
    });

    function rebuildIndex() {
      setIndexBusy(true);
      setIndexMessage(null);
      SDK.fetchJSON("/api/plugins/tool-slimmer/index/rebuild", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).then(function (result) {
        const rebuilt = result.index || {};
        setIndexMessage("Indexed " + String(rebuilt.total_tools || 0) + " tools from " + String(result.source || "Hermes") + ".");
        data.reload();
      }).catch(function (error) {
        setIndexMessage(error && error.message ? error.message : "Index rebuild failed");
      }).finally(function () {
        setIndexBusy(false);
      });
    }

    function generateEvalReport() {
      setEvalBusy(true);
      setEvalError(null);
      SDK.fetchJSON("/api/plugins/tool-slimmer/eval-report").then(function (result) {
        setEvalReport(result.report || {});
      }).catch(function (error) {
        setEvalError(error && error.message ? error.message : "Eval report failed");
      }).finally(function () {
        setEvalBusy(false);
      });
    }

    function applyAdvisorConfig() {
      setAdvisorBusy(true);
      setAdvisorMessage(null);
      SDK.fetchJSON("/api/plugins/tool-slimmer/advisor/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recommended_config: advisor.recommended_config || null }),
      }).then(function (result) {
        setAdvisorMessage("Applied config. Backup: " + String(result.backup_path || "created"));
        data.reload();
      }).catch(function (error) {
        setAdvisorMessage(error && error.message ? error.message : "Advisor apply failed");
      }).finally(function () {
        setAdvisorBusy(false);
      });
    }

    function setToolPreference(tool, action, profile) {
      if (!tool) return;
      setAdvisorBusy(true);
      setAdvisorMessage(null);
      SDK.fetchJSON("/api/plugins/tool-slimmer/advisor/tool-preference", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tool: tool, action: action, profile: profile || "default" }),
      }).then(function (result) {
        setAdvisorMessage(String(tool) + " saved to " + String(action) + ". Backup: " + String(result.backup_path || "created"));
        data.reload();
      }).catch(function (error) {
        setAdvisorMessage(error && error.message ? error.message : "Preference update failed");
      }).finally(function () {
        setAdvisorBusy(false);
      });
    }

    return React.createElement("div", { className: "tool-slimmer-page" },
      React.createElement("div", { className: "tool-slimmer-hero" },
        React.createElement("div", null,
          React.createElement("div", { className: "tool-slimmer-title-row" },
            React.createElement("h1", null, "Tool Slimmer"),
            React.createElement(Badge, { variant: config.enabled ? "default" : "outline" }, config.enabled ? "enabled" : "disabled"),
            advisor.status && React.createElement(Badge, { variant: advisor.status === "active" ? "default" : "outline" }, String(advisor.status).replaceAll("_", " ")),
            config.dry_run && React.createElement(Badge, { variant: "outline" }, "dry run"),
          ),
          React.createElement("p", null, "Reduces tool-schema overhead while keeping a full-tool fallback available."),
          React.createElement("div", { className: "tool-slimmer-muted text-sm" }, "Last selection: ", fmtTime(summary.last_event_at)),
        ),
        React.createElement(Button, { onClick: data.reload, disabled: data.loading }, data.loading ? "Refreshing" : "Refresh"),
      ),

      data.error && React.createElement(Card, { className: "border-destructive" },
        React.createElement(CardContent, { className: "py-4 text-sm" }, "Tool Slimmer API is not available: ", data.error),
      ),

      React.createElement("div", { className: "tool-slimmer-grid tool-slimmer-grid-primary" },
        React.createElement(Metric, {
          label: "Schema Tokens Saved",
          value: fmtNumber(totals.approx_tokens_saved),
          detail: "estimated from serialized schemas",
        }),
        React.createElement(Metric, {
          label: "Average Reduction",
          value: fmtPercent(averages.reduction_percent),
          detail: String(averages.selected_tools || 0) + " selected of " + String(averages.total_tools || 0),
        }),
        React.createElement(Metric, {
          label: "Selections Logged",
          value: fmtNumber(totals.events),
          detail: fmtNumber(totals.skipped_events || 0) + " guardrail skips",
        }),
        React.createElement(Metric, {
          label: "Selector Overhead",
          value: fmtMs(averages.selection_ms),
          detail: "average selection time",
        }),
      ),

      React.createElement("div", { className: "tool-slimmer-main-grid" },
        React.createElement(Card, null,
          React.createElement(CardHeader, { className: "tool-slimmer-card-header" },
            React.createElement(CardTitle, null, "Guided Setup"),
            React.createElement(Button, { onClick: applyAdvisorConfig, disabled: advisorBusy }, advisorBusy ? "Applying" : "Apply Config"),
          ),
          React.createElement(CardContent, { className: "grid gap-3 text-sm" },
            advisor.summary && React.createElement("div", { className: "tool-slimmer-muted" }, advisor.summary),
            React.createElement(CheckRows, { rows: setupChecklist }),
            advisorMessage && React.createElement("div", { className: "tool-slimmer-callout" }, advisorMessage),
            recommendations.length === 0 && React.createElement("div", { className: "tool-slimmer-callout" }, "No recommendations from recent selector activity."),
            recommendations.map(function (item) {
              return React.createElement("div", { key: item.id, className: "tool-slimmer-callout" },
                React.createElement("strong", null, String(item.id || "").replaceAll("_", " ")),
                React.createElement("div", { className: "tool-slimmer-muted text-xs" }, item.message),
              );
            }),
            advisor.recommended_yaml && React.createElement("details", { className: "tool-slimmer-details" },
              React.createElement("summary", null, "Applied config preview"),
              React.createElement("div", { className: "tool-slimmer-muted text-xs" },
                "This is the tool_slimmer config the advisor applies. You do not need to paste it anywhere after Apply Config succeeds.",
              ),
              React.createElement("pre", { className: "tool-slimmer-pre" }, advisor.recommended_yaml),
            ),
          ),
        ),

        React.createElement(Card, null,
          React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Tune Latest Selection")),
          React.createElement(CardContent, { className: "grid gap-3 text-sm" },
            latestSelected.length === 0 && React.createElement("div", { className: "tool-slimmer-muted" }, "No recent selected tools to tune yet."),
            latestSelected.length > 0 && React.createElement(React.Fragment, null,
              React.createElement("div", { className: "tool-slimmer-muted" }, "Use this when a recent decision clearly picked or missed a tool. Changes are scoped to ", React.createElement("span", { className: "font-courier" }, tuneProfile), "."),
              React.createElement(ToolPills, { tools: latestSelected, limit: 10 }),
              latestSelected.length > tuneTools.length && React.createElement("div", { className: "tool-slimmer-muted text-xs" }, "Showing the first ", String(tuneTools.length), " selected tools for tuning."),
              React.createElement("div", { className: "tool-slimmer-action-row" },
                React.createElement("select", {
                  className: "tool-slimmer-select",
                  value: selectedTuneTool,
                  onChange: function (event) { setTuneTool(event.target.value); },
                }, tuneTools.map(function (tool) {
                  return React.createElement("option", { key: tool, value: tool }, tool);
                })),
                React.createElement(Button, { variant: "outline", onClick: function () { setToolPreference(selectedTuneTool, "always_include", tuneProfile); }, disabled: advisorBusy || !selectedTuneTool }, "Always include"),
                React.createElement(Button, { variant: "outline", onClick: function () { setToolPreference(selectedTuneTool, "always_exclude", tuneProfile); }, disabled: advisorBusy || !selectedTuneTool }, "Never pick here"),
              ),
            ),
          ),
        ),
      ),

      React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Recent Decisions")),
        React.createElement(CardContent, { className: "overflow-x-auto" },
          React.createElement("table", { className: "tool-slimmer-table" },
            React.createElement("thead", null,
              React.createElement("tr", null,
                React.createElement("th", null, "Time"),
                React.createElement("th", null, "Mode"),
                React.createElement("th", null, "Reduction"),
                React.createElement("th", null, "Tools"),
                React.createElement("th", null, "Selected"),
              ),
            ),
            React.createElement("tbody", null,
              recent.slice().reverse().slice(0, 8).map(function (event, idx) {
                const metrics = event.metrics || {};
                return React.createElement("tr", { key: String(event.timestamp || idx) },
                  React.createElement("td", null, fmtTime(event.timestamp)),
                  React.createElement("td", { className: "font-courier" }, metrics.mode || "unknown"),
                  React.createElement("td", null,
                    fmtPercent(metrics.estimated_reduction_percent),
                    metrics.skipped && React.createElement("div", { className: "tool-slimmer-muted text-xs" }, metrics.skip_reason || "skipped"),
                  ),
                  React.createElement("td", null, String(metrics.selected_tools || 0), " / ", String(metrics.total_tools || 0)),
                  React.createElement("td", null, React.createElement(ToolPills, { tools: metrics.selected || [], limit: 5 })),
                );
              }),
              recent.length === 0 && React.createElement("tr", null,
                React.createElement("td", { colSpan: 5, className: "tool-slimmer-muted" }, "No selector decisions recorded yet."),
              ),
            ),
          ),
        ),
      ),

      React.createElement("div", { className: "tool-slimmer-main-grid" },
        React.createElement(Card, null,
          React.createElement(CardHeader, { className: "tool-slimmer-card-header" },
            React.createElement(CardTitle, null, "Tool Index"),
            React.createElement("div", { className: "tool-slimmer-action-row" },
              React.createElement(Button, { variant: "outline", onClick: data.reload, disabled: data.loading || indexBusy }, "Refresh"),
              React.createElement(Button, { onClick: rebuildIndex, disabled: indexBusy }, indexBusy ? "Rebuilding" : "Rebuild"),
            ),
          ),
          React.createElement(CardContent, { className: "grid gap-3 text-sm" },
            React.createElement("div", { className: "tool-slimmer-index-grid" },
              React.createElement("div", null, React.createElement("div", { className: "tool-slimmer-muted text-xs" }, "Status"), React.createElement("div", { className: "font-medium" }, index.exists ? "Ready" : "Not built")),
              React.createElement("div", null, React.createElement("div", { className: "tool-slimmer-muted text-xs" }, "Tools"), React.createElement("div", { className: "font-medium" }, String(index.total_tools || 0))),
              React.createElement("div", null, React.createElement("div", { className: "tool-slimmer-muted text-xs" }, "Updated"), React.createElement("div", { className: "font-medium" }, fmtIndexTime(index.updated_at))),
              React.createElement("div", null, React.createElement("div", { className: "tool-slimmer-muted text-xs" }, "Checksum"), React.createElement("div", { className: "font-courier" }, shortChecksum(index.checksum))),
            ),
            React.createElement("div", { className: "tool-slimmer-callout" },
              indexSource.platform
                ? "Indexed from the latest live " + indexSource.platform + " selector snapshot (" + String(indexSource.total_tools || indexSource.schema_count || 0) + " tools). Other Hermes entry points can legitimately have different tool counts until they run."
                : "This is an audit snapshot. Hermes selection uses the live request schemas in memory, so counts can differ by platform.",
            ),
            uniqueLiveSnapshots.length > 1 && React.createElement("div", { className: "tool-slimmer-snapshot-row" },
              uniqueLiveSnapshots.slice(0, 6).map(function (snapshot) {
                return React.createElement("span", { key: (snapshot.label || "") + ":" + (snapshot.platform || "") },
                  (snapshot.platform || snapshot.label || "unknown") + " " + String(snapshot.total_tools || 0),
                );
              }),
            ),
            React.createElement("div", { className: "tool-slimmer-path" }, index.path || "No index path available"),
            indexMessage && React.createElement("div", { className: "tool-slimmer-callout" }, indexMessage),
            indexDocs.length > 0 && React.createElement(ToolPills, { tools: indexDocs.slice(0, 18).map(function (doc) {
              return (doc.toolset ? doc.toolset + "." : "") + doc.name;
            }), limit: 18 }),
          ),
        ),

        React.createElement(Card, null,
          React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Most Selected")),
          React.createElement(CardContent, null,
            topTools.length === 0
              ? React.createElement("div", { className: "tool-slimmer-muted text-sm" }, "No selections have been recorded yet.")
              : React.createElement("div", { className: "tool-slimmer-tools" },
                  topTools.map(function ([tool, count]) {
                    return React.createElement("span", { key: tool, className: "tool-slimmer-pill" }, tool + " x" + count);
                  }),
                ),
          ),
        ),
      ),

      React.createElement("details", { className: "tool-slimmer-advanced" },
        React.createElement("summary", null, "Advanced diagnostics"),
        React.createElement("div", { className: "tool-slimmer-advanced-grid" },
          React.createElement(Card, null,
            React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Runtime")),
            React.createElement(CardContent, { className: "grid gap-2 text-sm" },
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Mode"), React.createElement("span", { className: "font-courier" }, config.mode || "unknown")),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Top K"), React.createElement("span", { className: "font-courier" }, String(config.top_k ?? "unknown"))),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Minimum Tools"), React.createElement("span", { className: "font-courier" }, String(config.min_total_tools ?? 0))),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Minimum Reduction"), React.createElement("span", { className: "font-courier" }, String(config.min_estimated_reduction_percent ?? 0) + "%")),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Two-pass Hydrate Limit"), React.createElement("span", { className: "font-courier" }, String(twoPass.hydrate_limit ?? "off"))),
            ),
          ),
          React.createElement(Card, null,
            React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Two-Pass")),
            React.createElement(CardContent, { className: "grid gap-2 text-sm" },
              config.mode !== "two_pass" && recentTwoPass.length === 0 && React.createElement("div", { className: "tool-slimmer-muted" }, "Experimental two-pass mode is not active."),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Recent Events"), React.createElement("span", { className: "font-courier" }, String(recentTwoPass.length))),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Last Phase"), React.createElement("span", { className: "font-courier" }, String(latestTwoPass.two_pass_phase || "none"))),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Fallbacks"), React.createElement("span", { className: "font-courier" }, String(twoPassFallbacks))),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Misses"), React.createElement("span", { className: "font-courier" }, String(twoPassMisses))),
              React.createElement("div", { className: "flex justify-between gap-3" }, React.createElement("span", { className: "tool-slimmer-muted" }, "Catalog Tokens"), React.createElement("span", { className: "font-courier" }, String(latestTwoPass.two_pass_catalog_approx_tokens || 0))),
              React.createElement("div", { className: "tool-slimmer-muted" }, "Hydrated"),
              React.createElement(ToolPills, { tools: latestTwoPass.two_pass_hydrated_tools || [], limit: 6 }),
            ),
          ),
          React.createElement(Card, null,
            React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Health Checks")),
            React.createElement(CardContent, null, React.createElement(CheckRows, { rows: doctorRows })),
          ),
          React.createElement(Card, null,
            React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Score Details")),
            React.createElement(CardContent, { className: "grid gap-2 text-sm" },
              latestCandidates.length === 0 && React.createElement("div", { className: "tool-slimmer-muted" }, "No score details recorded yet."),
              latestCandidates.slice(0, 6).map(function (candidate) {
                const details = candidate.details || {};
                return React.createElement("div", { key: candidate.name, className: "tool-slimmer-score-row" },
                  React.createElement("div", { className: "flex justify-between gap-3" },
                    React.createElement("span", { className: "font-courier" }, candidate.name),
                    React.createElement("span", null, (Number.isFinite(Number(candidate.score || 0)) ? Number(candidate.score || 0) : 0).toFixed(2)),
                  ),
                  React.createElement("div", { className: "tool-slimmer-muted text-xs" },
                    "bm25 ", (Number.isFinite(Number(details.bm25 || 0)) ? Number(details.bm25 || 0) : 0).toFixed(2),
                    " / name ", (Number.isFinite(Number(details.name_boost || 0)) ? Number(details.name_boost || 0) : 0).toFixed(2),
                    " / alias ", (Number.isFinite(Number(details.alias_boost || 0)) ? Number(details.alias_boost || 0) : 0).toFixed(2),
                    " / penalty ", (Number.isFinite(Number(details.context_penalty || 0)) ? Number(details.context_penalty || 0) : 0).toFixed(2),
                  ),
                );
              }),
            ),
          ),
          React.createElement(Card, null,
            React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Privacy")),
            React.createElement(CardContent, { className: "grid gap-3 text-sm" },
              React.createElement("div", { className: "flex flex-wrap gap-2" },
                React.createElement(Badge, { variant: privacy.raw_prompts_logged ? "outline" : "default" }, privacy.raw_prompts_logged ? "raw prompts logged" : "raw prompts not logged"),
                React.createElement(Badge, { variant: "outline" }, "session events only in headline"),
              ),
              React.createElement("div", { className: "tool-slimmer-path" }, privacy.decision_log_path || "No decision log path available"),
            ),
          ),
          React.createElement(Card, null,
            React.createElement(CardHeader, { className: "tool-slimmer-card-header" },
              React.createElement(CardTitle, null, "Release Evidence"),
              React.createElement(Button, { onClick: generateEvalReport, disabled: evalBusy }, evalBusy ? "Generating" : "Generate"),
            ),
            React.createElement(CardContent, { className: "grid gap-3 text-sm" },
              !evalReport && !evalError && React.createElement("div", { className: "tool-slimmer-muted" }, "Generate the bundled prompt/schema evaluation report."),
              evalError && React.createElement("div", { className: "tool-slimmer-muted" }, evalError),
              evalReport && evalReport.summary && React.createElement("div", { className: "tool-slimmer-index-grid" },
                React.createElement("div", null, React.createElement("div", { className: "tool-slimmer-muted text-xs" }, "Hit Rate"), React.createElement("div", { className: "font-medium" }, String(evalReport.summary.hit_rate))),
                React.createElement("div", null, React.createElement("div", { className: "tool-slimmer-muted text-xs" }, "Average Reduction"), React.createElement("div", { className: "font-medium" }, fmtPercent(evalReport.summary.average_reduction_percent))),
                React.createElement("div", null, React.createElement("div", { className: "tool-slimmer-muted text-xs" }, "Average Selected"), React.createElement("div", { className: "font-medium" }, String(evalReport.summary.average_selected_tools || 0))),
              ),
            ),
          ),
        ),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("tool-slimmer", ToolSlimmerPage);
})();
