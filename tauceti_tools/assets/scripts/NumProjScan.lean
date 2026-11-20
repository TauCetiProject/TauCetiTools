import Lean
/-!
Report numeric projections (`x.1`, `h.2`) that Mathlib's `linter.style.numericProjections`
would flag, without needing a Mathlib that has the linter. Elaborates each given file with
the given options and walks its info trees, using the linter's own detection logic.

`numproj_fix.py` compiles this driver once per toolchain and runs it under `lake env` with
`-Dopt=val ...` options and one file per process (a second `importModules` in one process is not
supported). Each finding is printed as `file<TAB>line<TAB>col<TAB>field` (line 1-based, col
0-based in codepoints, pointing at the projection's digits); an empty field marks a projection
whose elaborations disagree on the structure, which needs rewriting by hand.
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
  let mut byPos : Std.HashMap String.Pos.Raw (Array (Option Name)) := {}
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
                  return acc.push (r.start, field)
      return acc
    for (pos, field) in hits do
      unless byPos.contains pos do order := order.push pos
      byPos := byPos.alter pos fun fs => some ((fs.getD #[]).push field)
  for pos in order do
    let fields := byPos.getD pos #[]
    let p := fm.toPosition pos
    match fields[0]? with
    | some (some field) =>
      if fields.all (· == some field) then
        IO.println s!"{path}\t{p.line}\t{p.column}\t{field}"
      else
        IO.println s!"{path}\t{p.line}\t{p.column}\t"
    | _ =>
      unless fields.all (· == none) do
        IO.println s!"{path}\t{p.line}\t{p.column}\t"
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
