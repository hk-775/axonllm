"""Retained Cognito identity for first-time AxonLLM AgentCore adopters."""

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_cognito as cognito,
)
from constructs import Construct


TENANT_CLAIM_NAME = "custom:tenant_id"
PROJECT_CLAIM_NAME = "custom:project_id"


class AxonLLMIdentityStack(Stack):
    """Operator-managed workforce identity with no self-service enrollment."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        hosted_ui_domain_prefix = CfnParameter(
            self,
            "HostedUiDomainPrefix",
            type="String",
            min_length=3,
            max_length=63,
            allowed_pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$",
            constraint_description=(
                "must be 3-63 lowercase letters, numbers, or hyphens, "
                "starting and ending with a letter or number"
            ),
            description=(
                "Globally unique Cognito managed-login domain prefix"
            ),
        )
        oauth_callback_urls = CfnParameter(
            self,
            "OAuthCallbackUrls",
            type="CommaDelimitedList",
            allowed_pattern=r"^https://[^,\s#]+$",
            constraint_description=(
                "each callback must be an HTTPS URL without whitespace, "
                "commas, or fragments"
            ),
            description=(
                "Comma-separated OAuth authorization-code callback URLs"
            ),
        )

        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="axonllm-agentcore-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(
                    required=True,
                    mutable=True,
                ),
            ),
            custom_attributes={
                "tenant_id": cognito.StringAttribute(
                    min_len=1,
                    max_len=128,
                    mutable=True,
                ),
                "project_id": cognito.StringAttribute(
                    min_len=1,
                    max_len=128,
                    mutable=True,
                ),
            },
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            password_policy=cognito.PasswordPolicy(
                min_length=14,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(7),
            ),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(
                sms=False,
                otp=True,
            ),
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Your AxonLLM administrator invitation",
                email_body=(
                    "Your AxonLLM username is {username} and your temporary "
                    "password is {####}. Sign in and enroll TOTP MFA before "
                    "using the AgentCore runtime."
                ),
            ),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        readable_attributes = (
            cognito.ClientAttributes()
            .with_standard_attributes(
                email=True,
                email_verified=True,
            )
            .with_custom_attributes("tenant_id", "project_id")
        )
        writable_attributes = (
            cognito.ClientAttributes().with_standard_attributes(email=True)
        )
        app_client = user_pool.add_client(
            "PublicPkceClient",
            user_pool_client_name="axonllm-agentcore-pkce",
            generate_secret=False,
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            # The public client authenticates through authorization code plus
            # S256 PKCE. Direct password and SRP flows stay disabled.
            auth_flows=cognito.AuthFlow(),
            access_token_validity=Duration.minutes(15),
            id_token_validity=Duration.minutes(15),
            refresh_token_validity=Duration.hours(8),
            refresh_token_rotation_grace_period=Duration.seconds(0),
            read_attributes=readable_attributes,
            write_attributes=writable_attributes,
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            o_auth=cognito.OAuthSettings(
                callback_urls=oauth_callback_urls.value_as_list,
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=False,
                    client_credentials=False,
                ),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
            ),
        )
        app_client.apply_removal_policy(RemovalPolicy.RETAIN)

        hosted_ui_domain = user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=hosted_ui_domain_prefix.value_as_string,
            ),
            managed_login_version=(
                cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI
            ),
        )
        hosted_ui_domain.apply_removal_policy(RemovalPolicy.RETAIN)

        issuer = Fn.join(
            "",
            [
                "https://cognito-idp.",
                self.region,
                ".",
                self.url_suffix,
                "/",
                user_pool.user_pool_id,
            ],
        )
        discovery_url = Fn.join(
            "",
            [issuer, "/.well-known/openid-configuration"],
        )

        CfnOutput(
            self,
            "UserPoolId",
            value=user_pool.user_pool_id,
            description="Cognito user pool used by AxonLLM",
        )
        CfnOutput(
            self,
            "OidcIssuer",
            value=issuer,
            description="Exact OIDC issuer accepted by AxonLLM",
        )
        CfnOutput(
            self,
            "OidcDiscoveryUrl",
            value=discovery_url,
            description="OIDC discovery URL for the AgentCore authorizer",
        )
        CfnOutput(
            self,
            "OidcClientId",
            value=app_client.user_pool_client_id,
            description="Public authorization-code/PKCE client ID",
        )
        CfnOutput(
            self,
            "OidcAudience",
            value=app_client.user_pool_client_id,
            description="Expected Cognito ID-token audience",
        )
        CfnOutput(
            self,
            "HostedUiDomain",
            value=hosted_ui_domain.base_url(),
            description="Cognito hosted UI base URL",
        )
        CfnOutput(
            self,
            "TenantClaimName",
            value=TENANT_CLAIM_NAME,
        )
        CfnOutput(
            self,
            "ProjectClaimName",
            value=PROJECT_CLAIM_NAME,
        )
