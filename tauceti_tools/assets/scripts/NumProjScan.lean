import Lean
/-!
Report numeric projections (`x.1`, `h.2`) that Mathlib's `linter.style.numericProjections`
would flag, without needing a Mathlib that has the linter. Elaborates each given file with
the given options and walks its info trees, using the linter's own detection logic.

`numproj_fix.py` compiles this driver once per toolchain and runs it under `lake env` with
`-Dopt=val ...` options and one file per process (a second `importModules` in one process is not
supported). Each finding is printed as `file<TAB>line<TAB>col<TAB>field[<TAB>start:end]` (line
1-based, cols 0-based in codepoints, `col` pointing at the projection's digits). With `start:end`
the receiver, columns `start` to `end` (the `.` before the first rewritten projection), must be
parenthesized (see `receiverSpan`). A field `?` marks a projection whose elaborations disagree on
the structure or whose identifier cannot be parsed, and `!` (followed by the reason) one whose
named form might resolve to a type synonym's declaration (see `synonymCapture`); both need
rewriting by hand.
-/
open Lean Elab

/-- Structures exempt upstream (mirrors Mathlib's `upstreamAllowed`). -/
def allowedStructs : Array Name :=
  #[`Prod, `PProd, `Sigma, `PSigma, `And, `Iff,
    -- tagged `@[allow_numeric_proj]` in Mathlib (as of 35f49460)
    `QuaternionAlgebra, `Complex]

/-- For a structure projection function: `some none` if its structure is exempt, `some f` with
the field name `f` otherwise. `none` if `projFn` is not a structure projection. -/
def projField? (env : Environment) (projFn : Name) : Option (Option Name) := do
  let { ctorName, i, .. } ← env.getProjectionFnInfo? projFn
  let .ctorInfo { induct := structName, .. } ← env.find? ctorName | none
  let field ← (getStructureInfo? env structName).bind (·.fieldNames[i]?)
  return if allowedStructs.contains structName then none else some field

/-- One `.`-separated component of an identifier as written: its start column, its text
(without `«»`), and whether it is numeric. -/
structure Comp where
  col : Nat
  text : String
  numeric : Bool
  deriving Inhabited

/-- The components of the identifier chain (`a.b.1.c`) that contains column `col` of `line`,
following Lean's identifier lexing (escaped `«...»` components included), with the column where
the chain starts. `none` if it cannot be parsed. -/
def chainAt (line : Array Char) (col : Nat) : Option (Nat × Array Comp) := Id.run do
  -- Back to the chain's start.
  let mut start := col
  let mut fuel := line.size + 1
  while start > 0 && fuel > 0 do
    fuel := fuel - 1
    let c := line[start - 1]!
    if isIdRest c || c == '.' then
      start := start - 1
    else if c == '»' then
      let mut k := start - 1
      while k > 0 && line[k]! != '«' do k := k - 1
      if line[k]! != '«' then return none
      start := k
    else
      break
  -- A chain starting with `.` follows `)` or the like: its first component is the projection.
  while start < col && line[start]! == '.' do start := start + 1
  -- Forward through the components.
  let mut comps : Array Comp := #[]
  let mut i := start
  repeat
    if i < line.size && line[i]! == '«' then
      let mut j := i + 1
      while j < line.size && line[j]! != '»' do j := j + 1
      if j ≥ line.size then return none
      comps := comps.push ⟨i, (line.extract (i + 1) j).foldl String.push "", false⟩
      i := j + 1
    else
      let mut j := i
      while j < line.size && isIdRest line[j]! do j := j + 1
      if j == i then return none
      let text := (line.extract i j).foldl String.push ""
      comps := comps.push ⟨i, text, text.all Char.isDigit⟩
      i := j
    -- Continue only into a further component.
    if i + 1 < line.size && line[i]! == '.' && (isIdRest line[i + 1]! || line[i + 1]! == '«') then
      i := i + 1
    else
      break
  if comps.isEmpty then return none
  return some (start, comps)

/-- The receiver span to parenthesize for the projection whose digits start at `col` of line
`ln`, when its named form would not parse as intended. Written as an identifier, `s.1.foo`
becomes the single identifier `s.val.foo`; a global declaration that this name (from the
namespace and `open`s of any elaboration in `ctxs`) resolves to, consuming `val` or a later
component, would win over the projection, so the receiver must be written `(s).val.foo`. Also
always for an antiquotation (`$h.1`, since `$h.val` is `$(h.val)`), and conservatively when the
chain cannot be parsed. Numeric components that are findings count as rewritten (`agreed`); one
kept as is ends the identifier, after which only projection syntax follows. -/
def receiverSpan (ctxs : Array ContextInfo) (line : Array Char) (ln col : Nat)
    (agreed : Std.HashMap (Nat × Nat) Name) : IO (Option (Option (Nat × Nat))) := do
  let some (start, comps) := chainAt line col | return none
  let some k := comps.findIdx? (·.col == col) | return none
  -- After `)` and the like there is no identifier, only projection syntax.
  if comps[0]!.numeric then return some none
  -- The identifier segment containing component `k`.
  let mut segStart := 0
  for i in [0:k] do
    if comps[i]!.numeric && !agreed.contains (ln, comps[i]!.col) then segStart := i + 1
  if segStart > 0 then return some none
  let mut segEnd := comps.size
  for i in [k+1:comps.size] do
    if comps[i]!.numeric && !agreed.contains (ln, comps[i]!.col) && segEnd == comps.size then
      segEnd := i
  let some r0 := (comps.extract 0 segEnd).findIdx? (·.numeric) | return none
  let span := some (start, comps[r0]!.col - 1)
  if start > 0 && line[start - 1]! == '$' then return some (some (start - 1, comps[r0]!.col - 1))
  let candidate := (comps.extract 0 segEnd).foldl (init := Name.anonymous) fun n c =>
    if c.numeric then n ++ (agreed.getD (ln, c.col) .anonymous) else Name.str n c.text
  for ctx in ctxs do
    let resolutions ← try ctx.runCoreM (resolveGlobalName candidate) catch _ => pure [(.anonymous, [])]
    -- The projection parse keeps component `r0` and all later ones as field accesses.
    if resolutions.any (fun (_, fields) => fields.length < segEnd - r0) then return some span
  return some none

/-- Whether `e` applies projection function `fn` (with `nparams` parameters) to a receiver. -/
def isProjApp (fn : Name) (nparams : Nat) (e : Expr) : Bool :=
  e.isAppOf fn && e.getAppNumArgs > nparams

/-- Whether the named field `field` could resolve to a declaration `C.field` (as Lean's method
lookup resolves it) of a type synonym `C` for some receiver `x` of an application `fn ps x` in `e` (all are checked, since `e` may
contain several). Field notation looks for `C.field` on the head `C` of the receiver's type and
unfolds `C` only when that fails, while `.1` always reaches the structure. Returns what could
capture it (conservatively also when a receiver's type cannot be computed), or `none`. -/
def synonymCapture (ctx : ContextInfo) (lctx : LocalContext) (e : Expr) (fn : Name)
    (nparams : Nat) (field : Name) : IO (Option String) := do
  try
    ctx.runMetaM lctx do
      let found ← IO.mkRef none
      let checked ← IO.mkRef false
      let e ← instantiateMVars e
      -- Usually `e` is the projection itself; otherwise check every application of `fn` in it.
      let visit (k : Expr → MetaM Unit) : MetaM Unit :=
        if isProjApp fn nparams e then k e else e.forEach fun sub => do
          if isProjApp fn nparams sub then k sub
      visit fun sub => do
        let recv := sub.getAppArgs[nparams]!
        -- Under a binder of `e`, so not this projection, whose syntax would have its own term.
        if recv.hasLooseBVars then return
        checked.set true
        let mut ty ← Meta.whnfCore (← instantiateMVars (← Meta.inferType recv))
        for _ in [0:64] do
          let some C := ty.getAppFn.constName? | return
          if isStructure (← getEnv) C then return
          -- As Lean's `findMethod?`: `C.field` (for a private `C`, its user name), resolved
          -- without the current namespace but with the open ones; private names, aliases and
          -- `open`ed namespaces included.
          let resolved ← try
              withTheReader Core.Context ({ · with currNamespace := .anonymous }) do
                resolveGlobalName (privateToUserName C ++ field)
            catch _ => pure [(.anonymous, [])]
          if let some (n, _) := resolved.find? (·.2.isEmpty) then
            found.set (some s!"`{n}`")
            return
          let some ty' ← Meta.unfoldDefinition? ty | return
          ty ← Meta.whnfCore ty'
        found.set (some "a receiver type that does not unfold")
      if !(← checked.get) then return some "no elaborated receiver"
      found.get
  catch _ => return some "a receiver whose type could not be computed"

def parseOpt (s : String) : Option (Name × DataValue) := do
  let s ← s.dropPrefix? "-D"
  let [k, v] := s.toString.splitOn "=" | none
  let name := k.toName
  if v == "true" then return (name, .ofBool true)
  if v == "false" then return (name, .ofBool false)
  if let some n := v.toNat? then return (name, .ofNat n)
  return (name, .ofString v)

/-- Prints the findings for `path`; returns whether the file elaborated without errors. -/
def scanFile (opts : Options) (path : String) : IO Bool := do
  let input ← IO.FS.readFile path
  let inputCtx := Parser.mkInputContext input path
  let (header, parserState, messages) ← Parser.parseHeader inputCtx
  let (env, messages) ← processHeader header opts messages inputCtx
  if messages.hasErrors then
    for m in messages.toList do IO.eprintln (← m.toString)
    throw <| IO.userError "could not import the file's dependencies; build them first"
  let cmdState := { Command.mkState env messages opts with infoState := { enabled := true } }
  let s ← IO.processCommands inputCtx parserState cmdState
  let fm := inputCtx.fileMap
  -- Elaboration errors leave parts of the file unchecked; report them, but keep the findings.
  let mut clean := true
  for m in s.commandState.messages.toList do
    if m.severity == .error then
      IO.eprintln (← m.toString)
      clean := false
  -- A macro can elaborate one source projection several times, against different structures.
  -- Report a position only when every elaboration agrees; on a disagreement report an empty
  -- field, which numproj_fix.py lists for the author to rewrite by hand.
  -- Terms containing an application `fn ps x` of a projection function, from which a
  -- projection's receiver `x` is read.
  let mut apps : Array ((String.Pos.Raw × String.Pos.Raw) × ContextInfo × LocalContext × Expr) := #[]
  for t in s.commandState.infoState.trees do
    apps := apps ++ t.foldInfo (init := #[]) fun ctx info acc => Id.run do
      if let .ofTermInfo i := info then
        let hasProjApp := (i.expr.find? fun e => match e.getAppFn.constName? with
          | some fn => (ctx.env.getProjectionFnInfo? fn).any (e.getAppNumArgs > ·.numParams)
          | none => false).isSome
        if hasProjApp then
          if let some r := i.stx.getRange? (canonicalOnly := true) then
            return acc.push ((r.start, r.stop), ctx, i.lctx, i.expr)
      return acc
  -- A macro can elaborate one source projection several times. Report a position only when
  -- every elaboration agrees on the field and none of them would resolve the named field to a
  -- type synonym's declaration.
  let mut byPos : Std.HashMap String.Pos.Raw (Array (Option Name)) := {}
  let mut ctxs : Std.HashMap String.Pos.Raw (Array ContextInfo) := {}
  let mut synonym : Std.HashMap String.Pos.Raw String := {}
  let mut order : Array String.Pos.Raw := #[]
  for t in s.commandState.infoState.trees do
    let hits := t.foldInfo (init := #[]) fun ctx info acc => Id.run do
      if let .ofTermInfo i := info then
        if i.stx.isOfKind Parser.Term.identProjKind then
          let idx := i.stx[0]
          if idx.isOfKind fieldIdxKind then
            if let some fn := i.expr.constName? then
              if let some field := projField? ctx.env fn then
                if let some r := idx.getRange? (canonicalOnly := true) then
                  return acc.push (r, fn, field, ctx)
      return acc
    for (r, fn, field, ctx) in hits do
      let pos := r.start
      unless byPos.contains pos do order := order.push pos
      byPos := byPos.alter pos fun fs => some ((fs.getD #[]).push field)
      ctxs := ctxs.alter pos fun cs => some ((cs.getD #[]).push ctx)
      if let some f := field then
        -- The innermost terms around the digits that apply `fn`; without one, decline. All of
        -- that size are checked: a macro can elaborate the same syntax more than once, and
        -- nothing here ties a term to this hit's elaboration.
        let nparams := (ctx.env.getProjectionFnInfo? fn).map (·.numParams) |>.getD 0
        let mut inner : Array (ContextInfo × LocalContext × Expr) := #[]
        let mut innerSize := 0
        for ((a, b), actx, lctx, e) in apps do
          if a.byteIdx ≤ r.start.byteIdx && r.stop.byteIdx ≤ b.byteIdx then
            let size := b.byteIdx - a.byteIdx
            if (inner.isEmpty || size ≤ innerSize) && (e.find? (isProjApp fn nparams)).isSome then
              if !inner.isEmpty && size < innerSize then inner := #[]
              inner := inner.push (actx, lctx, e)
              innerSize := size
        let mut captured := if inner.isEmpty then some "no elaborated receiver" else none
        for (actx, lctx, e) in inner do
          if captured.isNone then captured ← synonymCapture actx lctx e fn nparams f
        if let some why := captured then synonym := synonym.insert pos why
  -- The field each position is rewritten to, keyed by (line, column).
  let mut agreed : Std.HashMap (Nat × Nat) Name := {}
  for pos in order do
    let fields := byPos.getD pos #[]
    if let some (some field) := fields[0]? then
      if fields.all (· == some field) && !synonym.contains pos then
        let p := fm.toPosition pos
        agreed := agreed.insert (p.line, p.column) field
  let lines := (input.splitOn "\n").toArray.map (·.toList.toArray)
  for pos in order do
    let p := fm.toPosition pos
    let row := s!"{path}\t{p.line}\t{p.column}\t"
    match agreed[(p.line, p.column)]? with
    | some field =>
      match ← receiverSpan (ctxs.getD pos #[]) (lines[p.line - 1]?.getD #[]) p.line p.column agreed with
      | some none => IO.println s!"{row}{field}"
      | some (some (a, b)) => IO.println s!"{row}{field}\t{a}:{b}"
      | none => IO.println s!"{row}?"
    | none =>
      if let some why := synonym[pos]? then
        IO.println s!"{row}!\t{why}"
      else unless (byPos.getD pos #[]).all (· == none) do
        IO.println s!"{row}?"
  return clean

unsafe def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  enableInitializersExecution
  let (optArgs, files) := args.partition (·.startsWith "-D")
  let opts := optArgs.foldl (init := ({} : Options)) fun o a =>
    match parseOpt a with | some (k, v) => o.insert k v | none => o
  -- 0: clean; 3: some file had elaboration errors (findings printed, maybe incomplete);
  -- 1: a file could not be processed at all.
  let mut status : UInt32 := 0
  for f in files do
    try
      unless ← scanFile opts f do
        if status == 0 then status := 3
    catch e =>
      IO.eprintln s!"{f}: {e}"
      status := 1
  return status
