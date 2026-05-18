import React, { createContext, useContext, useState } from "react";

interface FilterState {
  days: number;
  setDays: (d: number) => void;
}

const FilterContext = createContext<FilterState>({ days: 30, setDays: () => {} });

export function FilterProvider({ children }: { children: React.ReactNode }) {
  const [days, setDays] = useState(30);
  return (
    <FilterContext.Provider value={{ days, setDays }}>
      {children}
    </FilterContext.Provider>
  );
}

export const useFilter = () => useContext(FilterContext);
