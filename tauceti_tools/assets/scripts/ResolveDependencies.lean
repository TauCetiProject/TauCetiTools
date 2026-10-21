module

/-
Resolve inherited dependencies using Lake's own traversal and precedence, while keeping
every direct dependency at the authorized immutable revision in the seed manifest.
Run only in the isolated resolver workspace. Do not run post-update hooks.
-/
import all Lake.Load.Resolve
import Lake
import all Lake.CLI.Main

open Lake Lean

def resolvePins : CliM Unit := do
  let config ← mkLoadConfig { (← getThe LakeOptions) with configFile := "lakefile.toml" }
  let ws ← loadWorkspaceRoot config
  let manifest ← Manifest.load ws.manifestFile
  let pins := manifest.packages.foldl (init := ({} : NameMap PackageEntry)) fun pins entry =>
    if entry.inherited then pins else pins.insert entry.name entry
  let materialize pkg dep ws := do
    let materialized ← updateAndMaterializeDep ws pkg dep
    addDependencyEntries materialized
    return materialized
  let (ws, entries) ← (ws.resolveDepsCore materialize
    ws.root.wsIdx ws.wsIdx_root_lt ws.packages.size ws.wsIdx_root_lt
    (reconfigure := true)).run pins
  ws.writeManifest entries

public def main (args : List String) : IO UInt32 :=
  resolvePins.run args
