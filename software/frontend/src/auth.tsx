import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, setUnauthorizedHandler } from "./client";
import type { Session } from "./security";
import { authApi } from "./securityApi";

type AuthStatus = "booting" | "anonymous" | "authenticated";
type AuthContextValue = {
  status: AuthStatus;
  session: Session | null;
  bootError: string;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  refetchMe: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthStatus>("booting");
  const [session, setSession] = useState<Session | null>(null);
  const [bootError, setBootError] = useState("");

  const clearSession = useCallback(() => {
    queryClient.clear();
    setSession(null);
    setStatus("anonymous");
  }, [queryClient]);

  const refetchMe = useCallback(async () => {
    const next = await authApi.me();
    setSession(next);
    setBootError("");
    setStatus("authenticated");
  }, []);

  useEffect(() => {
    const removeHandler = setUnauthorizedHandler(clearSession);
    void refetchMe().catch((error: unknown) => {
      clearSession();
      if (!(error instanceof ApiError) || error.status !== 401) {
        setBootError(error instanceof Error ? error.message : "无法连接认证服务。");
      }
    });
    return removeHandler;
  }, [clearSession, refetchMe]);

  const login = useCallback(async (username: string, password: string) => {
    await authApi.login(username, password);
    await refetchMe();
  }, [refetchMe]);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } finally {
      clearSession();
    }
  }, [clearSession]);

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    await authApi.changePassword(currentPassword, newPassword);
    await refetchMe();
  }, [refetchMe]);

  const value = useMemo(() => ({
    status,
    session,
    bootError,
    login,
    logout,
    changePassword,
    refetchMe
  }), [bootError, changePassword, login, logout, refetchMe, session, status]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
