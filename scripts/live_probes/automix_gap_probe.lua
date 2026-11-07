-- Reaper-MCP automix live probe TEMPLATE: gen_automix_probe.py fills in the placeholder values
-- and writes a runnable copy into REAPER's Scripts folder.
--
-- Opens a synthetic two-deck project in a NEW project tab, measures what REAPER
-- really does with it, saves it, closes that tab, and returns to the tab you were
-- on. It never edits your own projects. Results go to @@OUT@@ (JSON) and to the
-- ExtState reaper_mcp_script_result/last_result ("PASS ..." or "FAIL ...").
--
-- Pass criteria (the constants automix_preview relies on):
--   grid        REAPER's tempo map matches the linear-ramp formula       <= 1e-9 s
--   deck/grid   each stretched deck's kicks sit on the grid              <= 1 ms
--   A vs B      the two decks agree with each other                      <= 1 ms
--   fade        item fade shape 1 follows x*(2-x)                        +-0.01
--   actions     41929/40859/40047/40295/40860 still have the expected names
local OUT, RPP, SAVE = "@@OUT@@", "@@RPP@@", "@@SAVE@@"
local TK = {@@TK@@}
local T = @@T@@
local SR = 44100
local R = {steps = {}}

local LIMITS = {grid_s = 1e-9, deck_ms = 1.0, ab_ms = 1.0, fade = 0.01}
local EXPECTED_ACTIONS = {
  ["41929"] = "New project tab", ["40859"] = "New project tab",
  ["40047"] = "Build any missing peaks", ["40295"] = "Zoom out project",
  ["40860"] = "Close current project tab",
}

local function ser(v, ind)
  ind = ind or ""
  local t = type(v)
  if t == "nil" then return "null"
  elseif t == "boolean" then return tostring(v)
  elseif t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    return string.format("%.12g", v)
  elseif t == "string" then
    return '"' .. v:gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end) .. '"'
  elseif t == "table" then
    local n = #v
    local parts = {}
    if n > 0 then
      for i = 1, n do parts[#parts + 1] = ser(v[i], ind .. " ") end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = tostring(k) end
    table.sort(keys)
    for _, k in ipairs(keys) do
      parts[#parts + 1] = "\n" .. ind .. " " .. ser(k) .. ": " .. ser(v[k] == nil and v[tonumber(k)] or v[k], ind .. " ")
    end
    return "{" .. table.concat(parts, ",") .. "\n" .. ind .. "}"
  end
  return '"?"'
end

local function step(name) R.steps[#R.steps + 1] = name end
local function count_tabs() local n = 0 while reaper.EnumProjects(n) do n = n + 1 end return n end

local function read_window(acc, t0, dur)
  local n = math.floor(dur * SR + 0.5)
  local buf = reaper.new_array(n)
  reaper.GetAudioAccessorSamples(acc, SR, 1, t0, n, buf)
  return buf.table()
end

-- First sample at 30% of the window peak, i.e. the kick onset.
local function onset(acc, expected)
  local t0 = expected - 0.04
  local x = read_window(acc, t0, 0.08)
  local peak = 0
  for i = 1, #x do local a = math.abs(x[i]) if a > peak then peak = a end end
  if peak < 1e-4 then return nil, peak end
  for i = 1, #x do
    if math.abs(x[i]) >= 0.3 * peak then return t0 + (i - 1) / SR, peak end
  end
end

local function mean_abs(acc, t0)
  local x = read_window(acc, t0, 0.01)
  local s = 0
  for i = 1, #x do s = s + math.abs(x[i]) end
  return s / #x
end

local function main()
  -- 1. action names (no side effects)
  R.actions = {}
  R.actions_ok = true
  for id, needle in pairs(EXPECTED_ACTIONS) do
    local text = reaper.kbd_getTextFromCmd(tonumber(id), 0)
    R.actions[id] = text
    if not tostring(text):find(needle, 1, true) then R.actions_ok = false end
  end
  R.app_version = reaper.GetAppVersion()
  step("names")
  if not R.actions_ok then error("an action changed its name; see actions") end

  -- 2. new tab + open (never touches the user's tab)
  local orig = reaper.EnumProjects(-1)
  R.tabs_before = count_tabs()
  reaper.Main_OnCommand(41929, 0)
  local newp = reaper.EnumProjects(-1)
  R.new_tab_is_different = newp ~= orig
  reaper.Main_openProject("noprompt:" .. RPP)
  local proj, fn = reaper.EnumProjects(-1)
  R.opened_file = fn
  R.same_tab_as_new = proj == newp
  R.dirty_after_open = reaper.IsProjectDirty(0)
  step("opened")

  local function track(i) return reaper.GetTrack(0, i) end

  -- 3. grid vs the ramp formula
  R.grid = {max_err_s = 0}
  for k = 0, #TK - 1 do
    local err = math.abs(reaper.TimeMap2_QNToTime(0, k) - TK[k + 1])
    if err > R.grid.max_err_s then R.grid.max_err_s = err end
  end
  step("grid")

  -- 4. stretched decks vs the grid, through track audio accessors
  local accR = reaper.CreateTrackAudioAccessor(track(2))
  local bias, nb = 0, 0
  for j = 0, 8 do
    local o = onset(accR, 10.0 + 0.5 * j)
    if o then bias = bias + (o - (10.0 + 0.5 * j)); nb = nb + 1 end
  end
  reaper.DestroyAudioAccessor(accR)
  bias = bias / math.max(1, nb)     -- the detector's own offset, measured on the unstretched reference
  local accA = reaper.CreateTrackAudioAccessor(track(0))
  local accB = reaper.CreateTrackAudioAccessor(track(1))
  R.sync = {detector_bias_ms = bias * 1000, max_a_ms = 0, max_b_ms = 0, max_ab_ms = 0, beats = 0}
  for k = 1, #TK - 2 do            -- skip the first and last beat (item edges)
    local oa = onset(accA, TK[k + 1])
    local ob = onset(accB, TK[k + 1])
    local ea = oa and (oa - TK[k + 1] - bias) * 1000
    local eb = ob and (ob - TK[k + 1] - bias) * 1000
    if ea and eb then
      R.sync.beats = R.sync.beats + 1
      R.sync.max_a_ms = math.max(R.sync.max_a_ms, math.abs(ea))
      R.sync.max_b_ms = math.max(R.sync.max_b_ms, math.abs(eb))
      R.sync.max_ab_ms = math.max(R.sync.max_ab_ms, math.abs(ea - eb))
    end
  end
  reaper.DestroyAudioAccessor(accA)
  reaper.DestroyAudioAccessor(accB)
  step("sync")

  -- 5. fade shape 1 through the accessor (DC 0.5, 4 s fade-in): gain at 25/50/75%
  local acc = reaper.CreateTrackAudioAccessor(track(3))
  local p = 20.0
  local ref = mean_abs(acc, p + 5.0)
  R.fade = {
    g25 = mean_abs(acc, p + 1.0 - 0.005) / ref,
    g50 = mean_abs(acc, p + 2.0 - 0.005) / ref,
    g75 = mean_abs(acc, p + 3.0 - 0.005) / ref,
    expected = {0.4375, 0.75, 0.9375},    -- x * (2 - x)
  }
  reaper.DestroyAudioAccessor(acc)
  step("fade")

  -- 6. save (so the tab closes without a prompt), close it, go back to the user's tab
  reaper.Main_SaveProjectEx(0, SAVE, 8)
  R.dirty_after_save = reaper.IsProjectDirty(0)
  R.closed_test_tab = false
  if R.dirty_after_save == 0 then
    reaper.Main_OnCommand(40860, 0)
    R.closed_test_tab = true
  end
  if orig then reaper.SelectProjectInstance(orig) end
  R.tabs_end = count_tabs()
  R.back_on_original = reaper.EnumProjects(-1) == orig
  step("done")

  -- verdict
  local f = R.fade
  local checks = {
    grid = R.grid.max_err_s <= LIMITS.grid_s,
    deck_a = R.sync.max_a_ms <= LIMITS.deck_ms,
    deck_b = R.sync.max_b_ms <= LIMITS.deck_ms,
    a_vs_b = R.sync.max_ab_ms <= LIMITS.ab_ms,
    fade_shape_1 = math.abs(f.g25 - f.expected[1]) <= LIMITS.fade and math.abs(f.g50 - f.expected[2]) <= LIMITS.fade
      and math.abs(f.g75 - f.expected[3]) <= LIMITS.fade,
    action_names = R.actions_ok,
    beats_measured = R.sync.beats >= 10,
    tab_handling = R.new_tab_is_different and R.same_tab_as_new and R.dirty_after_open == 0
      and R.closed_test_tab and R.back_on_original,
  }
  R.checks = checks
  R.passed = true
  for _, v in pairs(checks) do if not v then R.passed = false end end
end

local ok, err = pcall(main)
R.ok = ok
R.error = (not ok) and tostring(err) or nil
if not ok then R.passed = false end
local f = io.open(OUT, "w")
if f then f:write(ser(R)) f:close() end
reaper.SetExtState("reaper_mcp_script_result", "last_result",
  (R.passed and "PASS" or "FAIL") .. " steps=" .. #R.steps .. (ok and "" or (" error: " .. tostring(err))), false)
