-- Fixture for tests/test_compare_build_outputs.py. Compiled with Lean v4.37.0-rc1 as
-- `lean -jN Repro.lean -o Repro.olean`, which also writes .olean.server and .olean.private:
-- Repro.olean.private is from -j8 and Repro.j1.olean.private from -j1. Only the private
-- oleans differ, in the numbering of compiler-internal `_uniq` names.
module

public section

structure Eqv (α β : Type) where
  toFun : α → β
  invFun : β → α
  left_inv : ∀ a, invFun (toFun a) = a

theorem two_mul_div (t b : Int) (h : (t - b) % 2 = 0) : 2 * ((t - b) / 2) = t - b := by
  grind

def swap (t : Int) : Eqv {p : Int × Int // p.1 + p.2 = t} {p : Int × Int // p.2 + p.1 = t} where
  toFun p := ⟨(p.1.1, p.1.2), by grind⟩
  invFun p := ⟨(p.1.1, p.1.2), by grind⟩
  left_inv p := by grind
