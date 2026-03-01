# test_selectors.py
# Minimal test harness to demonstrate MRO + super() ordering

class BaseSelector:
    def __init__(self):
        print("BaseSelector.__init__")

    def select_gate(self):
        print("  BaseSelector.select_gate (noop). Class=%s" % self.__class__.__name__)

class PhysicalSelector(BaseSelector):
    def __init__(self):
        #print("PhysicalSelector.__init__ (before super)")
        super().__init__()
        print("PhysicalSelector.__init__ (after super)")

#    def select_gate(self):
#        # super-first pattern (so base and Virtual can run first in MRO unwind)
#        super().select_gate()
#        print("  PhysicalSelector.select_gate (no-op logic)")

class VirtualSelector(BaseSelector):
    def __init__(self):
        #print("VirtualSelector.__init__ (before super)")
        super().__init__()
        print("VirtualSelector.__init__ (after super)")

    def select_gate(self):
        # super-first so Base runs first
        super().select_gate()
        print("  VirtualSelector.select_gate: selecting gear stepper for gate. Class=%s" % self.__class__.__name__)
        # call mmu_toolhead to show effect

class LinearSelector(PhysicalSelector):
    def __init__(self):
        #print("LinearSelector.__init__ (before super)")
        super().__init__()
        print("LinearSelector.__init__ (after super)")

    def select_gate(self):
        # IMPORTANT: call super() first to let VirtualSelector select gear stepper
        super().select_gate()
        print("  LinearSelector.select_gate: move linear mechanism to gate. Class=%s" % self.__class__.__name__)

class LinearServoSelector(LinearSelector):
    def __init__(self):
        #print("LinearServoSelector.__init__ (before super)")
        super().__init__()
        print("LinearServoSelector.__init__ (after super)")

    def select_gate(self):
        # IMPORTANT: call super() first to let VirtualSelector select gear stepper
        super().select_gate()
        print("  LinearServoSelector.select_gate: move linear mechanism to gate")

class LinearMultiGearSelector(LinearServoSelector, VirtualSelector):
    def __init__(self):
        #print("LinearMultiGearSelector.__init__ (before super)")
        super().__init__()
        print("LinearMultiGearSelector.__init__ (after super)")

#    def select_gate(self):
#        # IMPORTANT: call super() first to let VirtualSelector select gear stepper
#        super().select_gate()
#        print("  LinearMultiGearSelector.select_gate: move linear mechanism to gate")

# --- end class definitions ---

if __name__ == "__main__":
    print("MRO for LinearMultiGearSelector:")
    import inspect
    print([c.__name__ for c in inspect.getmro(LinearMultiGearSelector)])
    print("\nInstantiate LinearMultiGearSelector:")
    sel = LinearMultiGearSelector()
    print("\nCall select_gate():")
    sel.select_gate()

    print("MRO for LinearServoSelector:")
    import inspect
    print([c.__name__ for c in inspect.getmro(LinearServoSelector)])
    print("\nInstantiate LinearServoSelector:")
    sel = LinearServoSelector()

    print("\nCall select_gate():")
    sel.select_gate()
