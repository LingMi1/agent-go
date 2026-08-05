// shadcn 约定的类名合并工具：clsx 组合 + tailwind-merge 去冲突。
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
