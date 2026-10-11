{{/*
Names. Everything is prefixed with the release name; service addresses are
always built from the release name AND namespace here, in one place - the
fix for the "half-templated namespace" problem in the sibling app chart.
*/}}
{{- define "webshop.name" -}}{{ .Release.Name }}-{{ .component }}{{- end }}

{{- define "webshop.fqdn" -}}
{{ .root.Release.Name }}-{{ .component }}.{{ .root.Release.Namespace }}.svc.cluster.local
{{- end }}

{{- define "webshop.image" -}}
{{- $i := .Values.image -}}
{{- if $i.digest -}}{{ $i.registry }}/{{ $i.repository }}@{{ $i.digest }}{{- else -}}{{ $i.registry }}/{{ $i.repository }}:{{ $i.tag }}{{- end -}}
{{- end }}

{{- define "webshop.labels" -}}
app.kubernetes.io/name: webshop
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/version: {{ .root.Values.image.tag | quote }}
app.kubernetes.io/part-of: webshop
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "webshop.selectorLabels" -}}
app.kubernetes.io/name: webshop
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* Secret names (generated or existing) */}}
{{- define "webshop.pgSecret" -}}
{{- .Values.postgresql.existingSecret | default (printf "%s-postgresql" .Release.Name) -}}
{{- end }}
{{- define "webshop.amqpSecret" -}}
{{- .Values.rabbitmq.existingSecret | default (printf "%s-rabbitmq" .Release.Name) -}}
{{- end }}

{{- define "webshop.pgHost" -}}
{{- if .Values.postgresql.enabled -}}{{ include "webshop.fqdn" (dict "root" . "component" "postgresql") }}{{- else -}}{{ required "postgresql.external.host is required when postgresql.enabled=false" .Values.postgresql.external.host }}{{- end -}}
{{- end }}
{{- define "webshop.amqpHost" -}}
{{- if .Values.rabbitmq.enabled -}}{{ include "webshop.fqdn" (dict "root" . "component" "rabbitmq") }}{{- else -}}{{ required "rabbitmq.external.host is required when rabbitmq.enabled=false" .Values.rabbitmq.external.host }}{{- end -}}
{{- end }}

{{- define "webshop.otlpEndpoint" -}}
{{- .Values.otel.endpoint | default (printf "http://%s-collector.%s.svc.cluster.local:4318" .Release.Name .Release.Namespace) -}}
{{- end }}

{{/*
A short hash of the non-secret application configuration. It becomes the
pods' webshop.config.revision resource attribute and a pod annotation, so a
config change both rolls the pods and is visible in every signal.
*/}}
{{- define "webshop.configRevision" -}}
{{- include (print .Template.BasePath "/configmap.yaml") . | sha256sum | trunc 12 -}}
{{- end }}

{{/* Restricted-profile container security context used by every pod. */}}
{{- define "webshop.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end }}

{{- define "webshop.podSecurity" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "webshop.scheduling" -}}
{{- with .Values.scheduling.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.scheduling.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.scheduling.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{- define "webshop.dnsConfig" -}}
{{- with .Values.dnsConfig }}
dnsConfig:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* Storage: a volumeClaimTemplate spec / PVC spec for a durable component. */}}
{{- define "webshop.pvcSpec" -}}
accessModes: [ReadWriteOnce]
{{- if eq .root.Values.storage.mode "localPV" }}
storageClassName: {{ .root.Values.storage.localPV.storageClassName }}
{{- else if .root.Values.storage.storageClassName }}
storageClassName: {{ .root.Values.storage.storageClassName }}
{{- end }}
resources:
  requests:
    storage: {{ .size }}
{{- end }}

{{/* NetworkPolicy peers: a webshop component, a subchart pod, the API server. */}}
{{- define "webshop.np.app" -}}
podSelector:
  matchLabels:
    app.kubernetes.io/instance: {{ .rel }}
    app.kubernetes.io/component: {{ .c }}
{{- end }}
{{- define "webshop.np.chart" -}}
podSelector:
  matchLabels:
    app.kubernetes.io/instance: {{ .rel }}
    app.kubernetes.io/name: {{ .n }}
{{- end }}
{{- define "webshop.np.apiserver" -}}
- to:
    {{- range .cidrs }}
    - ipBlock: {cidr: {{ . }}}
    {{- end }}
  ports:
    - {protocol: TCP, port: {{ .port }}}
{{- end }}
