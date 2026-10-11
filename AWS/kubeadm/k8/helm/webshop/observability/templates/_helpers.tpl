{{/*
Helpers used by the Helm-templated configuration files.
*/}}

{{/* Pod service discovery restricted to one namespace. */}}
{{- define "webshop-observability.podsd" }}
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names: [{{ .ns }}]
{{- end }}

{{/* Standard target labels for per-pod jobs. */}}
{{- define "webshop-observability.podlabels" }}
      - source_labels: [__meta_kubernetes_namespace]
        target_label: namespace
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
      - source_labels: [__meta_kubernetes_pod_node_name]
        target_label: node
{{- end }}

{{/* In-release service URLs, overridable for an existing stack. */}}
{{- define "webshop-observability.prometheusUrl" -}}
{{- .Values.grafana.urls.prometheus | default (printf "http://%s-prometheus-server" .Release.Name) -}}
{{- end }}
{{- define "webshop-observability.lokiUrl" -}}
{{- .Values.grafana.urls.loki | default (printf "http://%s-loki:3100" .Release.Name) -}}
{{- end }}
{{- define "webshop-observability.tempoUrl" -}}
{{- .Values.grafana.urls.tempo | default (printf "http://%s-tempo:3200" .Release.Name) -}}
{{- end }}

{{- define "webshop-observability.labels" -}}
app.kubernetes.io/name: webshop-observability
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: webshop
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
