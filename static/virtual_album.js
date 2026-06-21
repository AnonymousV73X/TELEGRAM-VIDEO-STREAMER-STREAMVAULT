// ── Virtual grid for album page ──────────────────────────────────────────────
// FIXED-POOL STRATEGY — eliminates scroll-up black flash entirely.
//
// The root cause of the black flash: the previous approach removed nodes from
// the DOM when they scrolled out, then re-inserted them on scroll-back. Every
// re-insertion triggers a fresh paint from a blank slate, which the browser
// can't complete before the next frame — so you see black for 1-2 frames.
//
// The fix (used by react-window, TanStack Virtual, and every production-grade
// virtualiser): keep a FIXED pool of N card nodes permanently in the DOM.
// Nodes are NEVER removed. When a slot scrolls out of view it gets:
//   • repositioned to its new logical index via transform
//   • its innerHTML swapped to the new card's pre-built string
//   • visibility:hidden during the swap, visible once placed
// Because the node stays on the compositor layer, the GPU never discards its
// texture. Scrolling back shows the repainted content instantly — no black.
//
// Other optimisations retained:
//   • gridTop cached (getBoundingClientRect only on init/resize)
//   • Real card height probed from a live node after first render
//   • All innerHTML strings pre-built at init time, never during scroll
//   • Velocity-predictive overscan (fast flings pre-populate leading edge)
(function () {
  // Inject lasso selection style once
  (function () {
    var s = document.createElement("style");
    s.textContent =
      ".video-card.lasso-selected{outline:1.5px solid #6c8fff;outline-offset:-2px;background:rgba(100,140,255,0.08);}";
    document.head.appendChild(s);
  })();

  // Ensure _selectedIds is always a Set (album page has no manage-page init)
  if (!(window._selectedIds instanceof Set)) window._selectedIds = new Set();

  var COLS = 4;
  var GAP = 14;
  var CARD_H = 300;

  // ── Cached layout (never read inside scroll RAF) ────────────────────────────
  var _gridTop = 0;
  var _colW = 0;

  function _recacheLayoutCore() {
    var w = grid.clientWidth || window.innerWidth;
    COLS = w < 500 ? 1 : w < 900 ? 2 : 4;
    _colW = (w - GAP * (COLS - 1)) / COLS;
    CARD_H = Math.round(_colW * (9 / 16)) + _BODY_H;
    _gridTop = grid.getBoundingClientRect().top + window.scrollY;
  }
  function _recacheLayout() {
    _recacheLayoutCore();
    _gridLeft = grid.getBoundingClientRect().left + window.scrollX;
  }

  // ── Velocity-predictive overscan ────────────────────────────────────────────
  var _vy = 0,
    _py = 0,
    _pt = 0;
  function _trackVel() {
    var t = performance.now(),
      dt = t - _pt;
    if (dt > 0 && dt < 200) _vy = (window.scrollY - _py) / dt;
    _py = window.scrollY;
    _pt = t;
  }
  function _overscan() {
    return 2 + Math.min(4, Math.floor(Math.abs(_vy) / 0.4));
  }

  // ── Pre-built HTML string cache ─────────────────────────────────────────────
  var _html = {}; // id -> innerHTML string

  var _bgs = [
    "linear-gradient(135deg,#0a0a0a 0%,#111008 60%,#080808 100%)",
    "linear-gradient(150deg,#080808 0%,#0d0d08 55%,#0a0a0a 100%)",
    "linear-gradient(120deg,#0a0a0a 0%,#0e0e0a 50%,#060606 100%)",
    "linear-gradient(160deg,#060606 0%,#0d0c08 60%,#0a0a0a 100%)",
    "linear-gradient(140deg,#080808 0%,#100f09 55%,#060606 100%)",
    "linear-gradient(125deg,#0a0a0a 0%,#0c0b07 50%,#080808 100%)",
  ];
  var _gp = ["20% 30%", "70% 20%", "50% 60%", "25% 70%", "75% 35%", "40% 50%"];
  var _go = [".18", ".14", ".16", ".15", ".17", ".13"];

  function _buildHtml(v) {
    var s = v.id % 6,
      ep = "";
    var m = (v.title || "").match(/[Ss](\d{1,2})[Ee](\d{1,3})/);
    if (!m) m = (v.title || "").match(/\b(\d{1,2})[xX](\d{2})\b/);
    if (m)
      ep =
        "S" +
        String(m[1]).padStart(2, "0") +
        " E" +
        String(m[2]).padStart(2, "0");
    var dur = v.dur ? '<span class="dur-badge">' + v.dur + "</span>" : "";
    var epb = ep ? '<span class="ep-badge">' + ep + "</span>" : "";
    var meta = "";
    if (v.quality)
      meta += '<span class="video-quality">' + v.quality + "</span>";
    if (v.dur)
      meta +=
        (meta ? '<span class="meta-dot">\xb7</span>' : "") +
        '<span class="video-duration">' +
        v.dur +
        "</span>";
    if (v.size)
      meta +=
        (meta ? '<span class="meta-dot">\xb7</span>' : "") +
        '<span class="video-size">' +
        v.size +
        "</span>";
    var mr = meta ? '<div class="video-meta-row">' + meta + "</div>" : "";
    var displayName =
      v.caption && v.caption.trim() ? v.caption.trim() : v.title;
    return (
      '<div class="video-art" onclick="openVLC(' +
      v.id +
      ',null)" style="cursor:pointer">' +
      '<div class="art-bg" style="' +
      _bgs[s] +
      ";background-image:radial-gradient(ellipse 70% 55% at " +
      _gp[s] +
      ",rgba(245,197,24," +
      _go[s] +
      ') 0%,transparent 70%)"></div>' +
      '<div class="art-vignette"></div>' +
      '<div class="play-overlay"><div class="play-circle"><svg viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg></div></div>' +
      dur +
      epb +
      "</div>" +
      '<div class="video-body"><div class="video-name">' +
      displayName +
      "</div>" +
      mr +
      '<div class="play-btn-row">' +
      '<button class="play-btn" onclick="openVLC(' +
      v.id +
      ',this)">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="11" height="11"><circle cx="12" cy="12" r="10"/><polygon points="10,8 16,12 10,16" fill="currentColor" stroke="none"/></svg>Play in VLC' +
      "</button>" +
      '<button class="play-btn-copy" title="Copy stream URL" onclick="event.stopPropagation();copyUrl(\'' +
      v.vlc +
      "',this)\">" +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="13" height="13"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>' +
      "</button>" +
      "</div>" +
      "</div>"
    );
  }

  // Lazy build: only generate HTML when a slot first needs it.
  // For large albums (500+ episodes) this cuts init time from O(n) to O(viewport).
  var _videoById = {};
  function _prebuild(videos) {
    _html = {};
    _videoById = {};
    for (var i = 0; i < videos.length; i++)
      _videoById[videos[i].id] = videos[i];
  }
  function _getHtml(id) {
    if (!_html[id])
      _html[id] = _buildHtml(_videoById[id] || { id: id, title: "" });
    return _html[id];
  }

  // ── Sort / filter ───────────────────────────────────────────────────────────
  var allVideos = [],
    filtered = [],
    activeSeason = 0;
  var _SS = "sv_alb_sort";
  var _sv = (function () {
    try {
      return JSON.parse(sessionStorage.getItem(_SS) || "null");
    } catch (e) {
      return null;
    }
  })();
  var sortKey = (_sv && _sv.key) || "name",
    sortDir = (_sv && _sv.dir) || 1; // episodes: name asc = ep order

  function _epKey(title) {
    var m = (title || "").match(/[Ss](\d{1,2})[Ee](\d{1,3})/);
    if (!m) m = (title || "").match(/\b(\d{1,2})[xX](\d{2})\b/);
    if (m) return parseInt(m[1], 10) * 10000 + parseInt(m[2], 10);
    return null;
  }
  function applySort(a) {
    return a.slice().sort(function (a, b) {
      var av, bv;
      if (sortKey === "date") {
        av = a.date || "";
        bv = b.date || "";
        return sortDir * (av < bv ? -1 : av > bv ? 1 : 0);
      }
      if (sortKey === "size") {
        return sortDir * ((a.size_bytes || 0) - (b.size_bytes || 0));
      }
      var ae = _epKey(a.title),
        be = _epKey(b.title);
      if (ae !== null && be !== null) return sortDir * (ae - be);
      if (ae !== null) return sortDir * -1;
      if (be !== null) return sortDir * 1;
      av = (a.title || "").toLowerCase();
      bv = (b.title || "").toLowerCase();
      return sortDir * (av < bv ? -1 : av > bv ? 1 : 0);
    });
  }
  function rebuildFiltered(q) {
    // Single pass over allVideos — combines season filter, text search, and
    // sort in one iteration instead of three separate .filter() allocations.
    var hasSeason = !!activeSeason;
    var hasQ = !!q;
    var b = [];
    for (var i = 0; i < allVideos.length; i++) {
      var v = allVideos[i];
      if (hasSeason && v.season !== activeSeason) continue;
      if (
        hasQ &&
        !(
          (v.title || "").toLowerCase().includes(q) ||
          (v.quality || "").toLowerCase().includes(q) ||
          (v.dur || "").toLowerCase().includes(q) ||
          (v.size || "").toLowerCase().includes(q)
        )
      )
        continue;
      b.push(v);
    }
    filtered = applySort(b);
  }

  window._albSetSeason = function (s) {
    activeSeason = s;
    document.querySelectorAll(".season-tab").forEach(function (t) {
      t.classList.toggle("active", parseInt(t.dataset.s, 10) === s);
    });
    var q = (document.getElementById("searchInput") || {}).value || "";
    rebuildFiltered(q.toLowerCase().trim());
    resetPool();
    window.scrollTo({ top: 0, behavior: "instant" });
    _recacheLayout();
    sentinel.style.height = totalH() + "px";
    render();
  };

  // ── DOM refs ────────────────────────────────────────────────────────────────
  var grid = document.getElementById("videosGrid");
  var sentinel = document.getElementById("vSentinel");

  // ── Layout maths ────────────────────────────────────────────────────────────
  function rowCount() {
    return Math.ceil(filtered.length / COLS);
  }
  function totalH() {
    return Math.max(0, rowCount() * (CARD_H + GAP) - GAP) + 80;
  }
  function cardTop(i) {
    return Math.floor(i / COLS) * (CARD_H + GAP);
  }
  function cardLeft(i) {
    return (i % COLS) * (_colW + GAP);
  }

  // ── FIXED POOL — nodes never leave the DOM ──────────────────────────────────
  // Each slot tracks which logical index it currently displays.
  // When a slot is reassigned it gets visibility:hidden for one frame while its
  // content is swapped, then becomes visible again — no black flash ever.
  var _slots = []; // [{el, idx, x, y, w, h}]  — permanent nodes in grid
  var _slotOf = {}; // logical index -> slot object (for fast lookup)
  var _freeList = []; // O(1) pop/push free-list — replaces O(n) linear scan
  var _POOL = 0; // pool size, set after card height probe

  function _allocPool(n) {
    while (_slots.length < n) {
      var el = document.createElement("div");
      el.className = "video-card";
      el.style.cssText =
        "position:absolute;visibility:hidden;width:0;transform:translate(-9999px,0);";
      grid.appendChild(el);
      var s = { el: el, idx: -1, x: -9999, y: 0, w: 0, h: 0 };
      _slots.push(s);
      _freeList.push(s);
    }
    _POOL = _slots.length;
  }

  function resetPool() {
    _slotOf = {};
    _freeList = [];
    for (var i = 0; i < _slots.length; i++) {
      _slots[i].idx = -1;
      _slots[i].el.style.visibility = "hidden";
      _freeList.push(_slots[i]);
    }
  }

  // O(1) — evicted slots were already pushed onto _freeList in _renderCore
  function _freeSlot(firstIdx, lastIdx) {
    return _freeList.length ? _freeList.pop() : null;
  }

  // ── Card height — computed from known CSS values, no DOM probe needed ─────────
  // art: 16/9 * _colW
  // body: padding-top 12 + padding-bottom 16 = 28
  //       video-name min-height: 0.85rem * 1.4 * 3 ≈ 51px (at 16px root)
  //       video-meta-row: ~22px
  //       gap between elements: 3 * 5 = 15px
  //       play-btn-row: padding-top 10 + height 32 = 42px
  // total body ≈ 28 + 51 + 22 + 15 + 42 = 158px
  var _BODY_H = 158;
  var _probedCols = -1; // kept for API compat with onResize reset
  function _probe() {
    // No-op: height is computed deterministically in _recacheLayout
  }

  // ── Render ──────────────────────────────────────────────────────────────────
  function _renderCore() {
    var sy = window.scrollY,
      vpH = window.innerHeight,
      ov = _overscan();
    var rowH = CARD_H + GAP;
    var rel = sy - _gridTop;
    var fr = Math.max(0, Math.floor((rel - ov * rowH) / rowH));
    var lr = Math.ceil((rel + vpH + ov * rowH) / rowH);
    var fi = fr * COLS,
      li = Math.min(filtered.length - 1, (lr + 1) * COLS - 1);

    // Hide slots that are now out of the window
    for (var k in _slotOf) {
      var ki = parseInt(k);
      if (ki < fi || ki > li) {
        _slotOf[k].el.style.visibility = "hidden";
        _slotOf[k].idx = -1;
        _slotOf[k].x = -9999;
        _slotOf[k].y = 0;
        _freeList.push(_slotOf[k]); // return to free-list — O(1) later retrieval
        delete _slotOf[k];
      }
    }

    // Assign/reuse slots for cards now in the window
    for (var i = fi; i <= li; i++) {
      if (_slotOf[i]) continue; // already rendered — just reposition below
      var v = filtered[i];
      if (!v) continue;
      var slot = _freeSlot(fi, li);
      if (!slot) continue; // pool exhausted (shouldn't happen with correct sizing)
      // Unregister old index
      if (slot.idx >= 0) delete _slotOf[slot.idx];
      // Swap content
      slot.el.style.visibility = "hidden";
      slot.el.innerHTML = _getHtml(v.id);
      slot.idx = i;
      _slotOf[i] = slot;
    }

    // Position every assigned slot — only write style when value actually changed
    for (var j in _slotOf) {
      var ji = parseInt(j),
        sl = _slotOf[j];
      var tx = cardLeft(ji),
        ty = cardTop(ji);
      if (sl.w !== _colW || sl.h !== CARD_H) {
        sl.el.style.width = _colW + "px";
        sl.el.style.height = CARD_H + "px";
        sl.w = _colW;
        sl.h = CARD_H;
      }
      if (sl.x !== tx || sl.y !== ty) {
        sl.el.style.transform = "translate(" + tx + "px," + ty + "px)";
        sl.x = tx;
        sl.y = ty;
      }
      sl.el.style.visibility = "visible";
    }

    sentinel.style.height = totalH() + "px";
  }

  // ── Resize ──────────────────────────────────────────────────────────────────
  // Trailing-edge only: ignore all events during the drag, commit once
  // 120ms after the last one fires. No rAF chaining — one clean recache.
  var _rt = null;
  function _doResize() {
    _probedCols = -1;
    _recacheLayout();
    _probe();
    for (var i = 0; i < _slots.length; i++) {
      _slots[i].el.style.width = _colW + "px";
      _slots[i].el.style.height = CARD_H + "px";
    }
    sentinel.style.height = totalH() + "px";
    resetPool();
    render();
  }
  function onResize() {
    clearTimeout(_rt);
    _rt = setTimeout(_doResize, 120);
  }

  // ── Sort buttons ────────────────────────────────────────────────────────────
  var _SL = { date: "Date Added", name: "Name", size: "Size" };
  function updateSortUI() {
    ["date", "name", "size"].forEach(function (k) {
      var b = document.getElementById("albSort_" + k);
      if (!b) return;
      b.textContent =
        _SL[k] +
        (sortKey === k ? (sortDir === -1 ? " \u25be" : " \u25b4") : "");
      b.className = "alb-sort-btn" + (sortKey === k ? " active" : "");
    });
  }
  window._albSetSort = function (key) {
    if (sortKey === key) {
      sortDir = -sortDir;
    } else {
      sortKey = key;
      sortDir = key === "date" ? -1 : 1;
    }
    try {
      sessionStorage.setItem(
        _SS,
        JSON.stringify({ key: sortKey, dir: sortDir }),
      );
    } catch (e) {}
    var q = (document.getElementById("searchInput") || {}).value || "";
    rebuildFiltered(q.toLowerCase().trim());
    resetPool();
    window.scrollTo({ top: 0, behavior: "instant" });
    _recacheLayout();
    sentinel.style.height = totalH() + "px";
    updateSortUI();
    render();
  };

  // ── Search ──────────────────────────────────────────────────────────────────
  var _st = null;
  document.getElementById("searchInput").addEventListener("input", function () {
    var q = this.value.toLowerCase().trim();
    clearTimeout(_st);
    _st = setTimeout(function () {
      rebuildFiltered(q);
      resetPool();
      window.scrollTo({ top: 0, behavior: "instant" });
      _recacheLayout();
      sentinel.style.height = totalH() + "px";
      render();
    }, 80);
  });

  // ── Scroll handler ──────────────────────────────────────────────────────────
  var _raf = false;
  window.addEventListener(
    "scroll",
    function () {
      _trackVel();
      if (_raf) return;
      if (_rt) return; // resize pending — _gridTop is stale, skip
      _raf = true;
      requestAnimationFrame(function () {
        _raf = false;
        render();
      });
    },
    { passive: true },
  );

  window.addEventListener("resize", onResize);

  // ── Lasso selection ─────────────────────────────────────────────────────────
  // All coords stored as page-coords (pageX/pageY = clientX + scrollY).
  // Grid-relative conversion: pageY - _gridTop, pageX - _gridLeft.
  // _gridLeft is cached alongside _gridTop in _recacheLayout.
  // Auto-scroll: each RAF frame checks mouse clientY vs viewport edges,
  // calls window.scrollBy — page coords of mouse stay valid, _gridTop shifts
  // only on explicit resize, so no coord adjustment needed after scroll.
  var _EDGE_PX = 52;
  var _SCROLL_MAX = 20;
  var _gridLeft = 0;

  function _lassoEnsure() {
    if (_lasso) return;
    _lasso = document.createElement("div");
    _lasso.style.cssText =
      "position:absolute;pointer-events:none;z-index:9000;" +
      "border:1.5px solid #6c8fff;background:rgba(100,140,255,0.10);" +
      "display:none;box-sizing:border-box;";
    grid.appendChild(_lasso);
  }

  function _lassoApplySelected() {
    for (var k in _slotOf) {
      var sl = _slotOf[k];
      if (sl && sl.el && sl.idx >= 0 && filtered[sl.idx])
        sl.el.classList.toggle(
          "lasso-selected",
          window._selectedIds.has(filtered[sl.idx].id),
        );
    }
  }

  function _lassoFrame() {
    if (!_lasso_active) return;

    // Auto-scroll: use clientY (viewport-relative) derived from pageY - scrollY
    var clientY = _lasso_cy - window.scrollY;
    var delta = 0;
    if (clientY < _EDGE_PX && clientY >= 0) {
      delta = -Math.round(_SCROLL_MAX * Math.pow(1 - clientY / _EDGE_PX, 1.5));
    } else if (
      clientY > window.innerHeight - _EDGE_PX &&
      clientY <= window.innerHeight
    ) {
      var excess = clientY - (window.innerHeight - _EDGE_PX);
      delta = Math.round(_SCROLL_MAX * Math.pow(excess / _EDGE_PX, 1.5));
    }
    if (delta !== 0) window.scrollBy(0, delta);
    // Note: after scrollBy, window.scrollY changed but _lasso_cy (pageY) is
    // unchanged — page coords are scroll-independent, so the rect stays correct.

    // Convert page coords → grid-relative coords (no DOM read)
    var sx = _lasso_sx - _gridLeft;
    var sy = _lasso_sy - _gridTop;
    var cx = _lasso_cx - _gridLeft;
    var cy = _lasso_cy - _gridTop;
    var l = Math.min(sx, cx),
      r = Math.max(sx, cx);
    var t = Math.min(sy, cy),
      b = Math.max(sy, cy);

    // Position overlay
    _lasso.style.cssText =
      "position:absolute;pointer-events:none;z-index:9000;" +
      "border:1.5px solid #6c8fff;background:rgba(100,140,255,0.10);" +
      "box-sizing:border-box;" +
      "left:" +
      l +
      "px;top:" +
      t +
      "px;" +
      "width:" +
      Math.max(0, r - l) +
      "px;height:" +
      Math.max(0, b - t) +
      "px;";

    // Hit-test all items using layout math only
    for (var i = 0; i < filtered.length; i++) {
      var vid = filtered[i];
      if (!vid) continue;
      var cl = cardLeft(i),
        ct = cardTop(i);
      var hit = cl < r && cl + _colW > l && ct < b && ct + CARD_H > t;
      if (hit) window._selectedIds.add(vid.id);
      else window._selectedIds.delete(vid.id);
    }
    _lassoApplySelected();

    _lasso_raf = requestAnimationFrame(_lassoFrame);
  }

  function _lassoStart(e) {
    if (e.button !== 0) return;
    // Bail if the click landed inside a card
    var t = e.target;
    while (t && t !== grid) {
      if (t.classList && t.classList.contains("video-card")) return;
      t = t.parentNode;
    }
    e.preventDefault();
    document.body.style.userSelect = "none";
    document.body.style.webkitUserSelect = "none";
    _lassoEnsure();
    _lasso_active = true;
    _lasso_sx = _lasso_cx = e.pageX;
    _lasso_sy = _lasso_cy = e.pageY;
    if (!e.shiftKey) {
      window._selectedIds.clear();
      for (var k in _slotOf)
        if (_slotOf[k] && _slotOf[k].el)
          _slotOf[k].el.classList.remove("lasso-selected");
    }
    cancelAnimationFrame(_lasso_raf);
    _lasso_raf = requestAnimationFrame(_lassoFrame);
    document.addEventListener("mousemove", _lassoMove, { passive: true });
    document.addEventListener("mouseup", _lassoEnd, { once: true });
  }

  function _lassoMove(e) {
    _lasso_cx = e.pageX;
    _lasso_cy = e.pageY;
  }

  function _lassoEnd() {
    _lasso_active = false;
    cancelAnimationFrame(_lasso_raf);
    document.removeEventListener("mousemove", _lassoMove);
    document.body.style.userSelect = "";
    document.body.style.webkitUserSelect = "";
    if (_lasso) _lasso.style.display = "none";
  }

  // render() — public entry point; re-stamps .lasso-selected after slot recycling
  function render() {
    _renderCore();
    if (window._selectedIds.size > 0) _lassoApplySelected();
  }

  // ── Init ────────────────────────────────────────────────────────────────────
  window._initVirtualGrid = function (data) {
    allVideos = data || [];
    _prebuild(allVideos);

    // Preserve any query the user typed before data arrived
    var _q = ((document.getElementById("searchInput") || {}).value || "")
      .toLowerCase()
      .trim();

    // Season tabs
    var seasons = [];
    allVideos.forEach(function (v) {
      if (v.season && seasons.indexOf(v.season) < 0) seasons.push(v.season);
    });
    seasons.sort(function (a, b) {
      return a - b;
    });
    var tb = document.getElementById("seasonTabBar");
    if (tb && seasons.length > 1) {
      tb.style.display = "flex";
      activeSeason = seasons[0];
      var h = "";
      seasons.forEach(function (s) {
        h +=
          '<button class="season-tab' +
          (s === seasons[0] ? " active" : "") +
          '" data-s="' +
          s +
          '" onclick="_albSetSeason(' +
          s +
          ')">Season ' +
          s +
          "</button>";
      });
      tb.innerHTML = h;
    }

    rebuildFiltered(_q);
    updateSortUI();
    grid.style.position = "relative";
    grid.style.userSelect = "none";
    grid.addEventListener("mousedown", _lassoStart);
    grid.addEventListener("selectstart", function (e) {
      if (_lasso_active) e.preventDefault();
    });
    _recacheLayout();

    var vpRows = Math.ceil(window.innerHeight / CARD_H) + 8;
    var poolN = Math.min(allVideos.length, vpRows * COLS);
    _allocPool(Math.max(poolN, 12));

    // Probe actual card height before first render so CARD_H is accurate
    _probe();

    sentinel.style.height = totalH() + "px";
    render();
  };

  // Called by the hero poster img onload to correct _gridTop after any
  // layout shift caused by the image expanding its container.
  window._recacheGridTop = function () {
    _recacheLayout();
    render();
  };

  grid.style.position = "relative";
  _recacheLayout();
  updateSortUI();
})();
