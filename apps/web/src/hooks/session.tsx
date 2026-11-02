import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/resources";
import { ApiError, authEvents, setCsrf } from "../api/client";
import type { Schema } from "../api/types";
import { LoginPage } from "../pages/LoginPage";
import { ErrorNotice, Loading } from "../components/common";
const SessionContext = createContext<Schema["SessionView"] | null>(null);
export function SessionProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const session = useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      try {
        const value = await api.session();
        setCsrf(value.csrf_token);
        return value;
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    retry: false,
    staleTime: 60000,
    refetchInterval: 60000,
  });
  useEffect(() => {
    const expire = () => {
      setCsrf(null);
      client.cancelQueries({
        predicate: (query) => query.queryKey[0] !== "session",
      });
      client.removeQueries({
        predicate: (query) => query.queryKey[0] !== "session",
      });
      client.setQueryData(["session"], null);
    };
    authEvents.addEventListener("expired", expire);
    return () => authEvents.removeEventListener("expired", expire);
  }, [client]);
  if (session.isPending) return <Loading />;
  if (session.error)
    return (
      <div className="login-shell">
        <ErrorNotice error={session.error} retry={() => session.refetch()} />
      </div>
    );
  if (!session.data)
    return (
      <LoginPage
        onSuccess={(value) => {
          setCsrf(value.csrf_token);
          client.cancelQueries();
          client.removeQueries({
            predicate: (query) => query.queryKey[0] !== "session",
          });
          client.setQueryData(["session"], value);
        }}
      />
    );
  return (
    <SessionContext.Provider value={session.data}>
      {children}
    </SessionContext.Provider>
  );
}
export function useSession() {
  return useContext(SessionContext);
}
