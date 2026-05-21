import "next-auth";

declare module "next-auth" {
  interface User {
    uc_uid?: string;
  }

  interface Session {
    user: User;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    uc_uid?: string;
  }
}
