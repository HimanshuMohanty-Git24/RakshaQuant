import { create } from "zustand";

/** App-wide overlays that more than one place can open (the palette, the HALT dialog). */
interface Ui {
  palette: boolean;
  halt: boolean;
  setPalette: (open: boolean) => void;
  setHalt: (open: boolean) => void;
}

export const useUi = create<Ui>((set) => ({
  palette: false,
  halt: false,
  setPalette: (palette) => set({ palette }),
  setHalt: (halt) => set({ halt }),
}));
