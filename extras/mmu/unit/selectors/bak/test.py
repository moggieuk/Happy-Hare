# test_selectors.py
# Minimal test harness to demonstrate MRO + super() ordering

class BaseSelector:
    def __init__(self):
        print("BaseSelector.__init__")

    # Prevent overriding of methods with logical gate number
    _final_methods = {"select_gate", "restore_gate"}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        overridden = BaseSelector._final_methods.intersection(cls.__dict__.keys())
        if overridden:
            raise TypeError(
                f"{cls.__name__} is not allowed to override: {', '.join(sorted(overridden))}"
            )

    def select_gate(self, gate):
        print("  BaseSelector.select_gate(%d). Class=%s" % (gate, self.__class__.__name__))
        return self._select_gate(gate - 1)

    def _select_gate(self, lgate):
        print("  BaseSelector._select_gate(%d) called !!!!!!!!!!!" % lgate)

class PhysicalSelector(BaseSelector):
    def __init__(self):
        super().__init__()
        print("PhysicalSelector.__init__ (after super)")

    def _select_gate(self, lgate):
        super()._select_gate(lgate)


class VirtualSelector(BaseSelector):
    def __init__(self):
        super().__init__()
        print("VirtualSelector.__init__ (after super)")

    def _select_gate(self, lgate):
        # super-first so Base runs first
        super()._select_gate(lgate)
        print("  VirtualSelector._select_gate(%d): selecting gear stepper for gate. Class=%s" % (lgate, self.__class__.__name__))
        # call mmu_toolhead to show effect


class LinearSelector(PhysicalSelector):
    def __init__(self):
        super().__init__()
        print("LinearSelector.__init__ (after super)")

    def _select_gate(self, lgate):
        super()._select_gate(lgate) # IMPORTANT: call super() first to let VirtualSelector select gear stepper
        print("  LinearSelector._select_gate(%d): move linear mechanism to gate. Class=%s" % (lgate, self.__class__.__name__))


class LinearServoSelector(LinearSelector):
    def __init__(self):
        super().__init__()
        print("LinearServoSelector.__init__ (after super)")

    def _select_gate(self, lgate):
        super()._select_gate(lgate) # IMPORTANT: call super() first to let VirtualSelector select gear stepper
        print("  LinearServoSelector._select_gate(%d): move linear mechanism to gate" % lgate)


class LinearMultiGearSelector(LinearServoSelector, VirtualSelector):
    def __init__(self):
        super().__init__()
        print("LinearMultiGearSelector.__init__ (after super)")

# --- end class definitions ---

if __name__ == "__main__":
    print("MRO for LinearMultiGearSelector:")
    import inspect
    print([c.__name__ for c in inspect.getmro(LinearMultiGearSelector)])
    print("\nInstantiate LinearMultiGearSelector:")
    sel = LinearMultiGearSelector()
    print("\nCall select_gate():")
    sel.select_gate(10)

    print("\n-----\n")

    print("MRO for LinearServoSelector:")
    import inspect
    print([c.__name__ for c in inspect.getmro(LinearServoSelector)])
    print("\nInstantiate LinearServoSelector:")
    sel = LinearServoSelector()
    print("\nCall select_gate():")
    sel.select_gate(10)

    print("\n-----\n")

    print("MRO for LinearSelector:")
    import inspect
    print([c.__name__ for c in inspect.getmro(LinearSelector)])
    print("\nInstantiate LinearSelector:")
    sel = LinearSelector()
    print("\nCall select_gate():")
    sel.select_gate(10)

    print("\n-----\n")

    print("MRO for VirtualSelector:")
    import inspect
    print([c.__name__ for c in inspect.getmro(VirtualSelector)])
    print("\nInstantiate VirtualSelector:")
    sel = VirtualSelector()
    print("\nCall select_gate():")
    sel.select_gate(10)
