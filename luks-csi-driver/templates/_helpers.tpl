{{/*
Expand the chart name.
*/}}
{{- define "luks-csi-driver.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name, truncated to 63 chars.
If the release name already contains the chart name it is used as-is.
*/}}
{{- define "luks-csi-driver.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label (name-version).
*/}}
{{- define "luks-csi-driver.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "luks-csi-driver.labels" -}}
helm.sh/chart: {{ include "luks-csi-driver.chart" . }}
{{ include "luks-csi-driver.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (stable — used in matchLabels and must not change after first deploy).
*/}}
{{- define "luks-csi-driver.selectorLabels" -}}
app.kubernetes.io/name: {{ include "luks-csi-driver.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Controller ServiceAccount name.
*/}}
{{- define "luks-csi-driver.controllerServiceAccount" -}}
{{- printf "%s-controller" (include "luks-csi-driver.fullname" .) }}
{{- end }}

{{/*
Node ServiceAccount name.
*/}}
{{- define "luks-csi-driver.nodeServiceAccount" -}}
{{- printf "%s-node" (include "luks-csi-driver.fullname" .) }}
{{- end }}

{{/*
Driver image reference (repository:tag).
*/}}
{{- define "luks-csi-driver.image" -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}

{{/*
Vault environment variables — injected into both controller and node containers.
*/}}
{{- define "luks-csi-driver.vaultEnv" -}}
- name: VAULT_ADDR
  value: {{ .Values.vault.address | quote }}
- name: VAULT_ROLE
  value: {{ .Values.vault.role | quote }}
- name: VAULT_MOUNT
  value: {{ .Values.vault.mount | quote }}
{{- end }}
