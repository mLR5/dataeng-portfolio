# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : règles
# MAGIC
# MAGIC Contrat des deux tables, registre des villes, contrôles de qualité, transformation
# MAGIC d'un JSON Bronze et contrôles de cohérence. Python pur, sans Spark ni I/O :
# MAGIC ce notebook est chargé par `%run` et testé en CI.

# COMMAND ----------

# MAGIC %run ./silver_bronze_contract

# COMMAND ----------

# MAGIC %run ./silver_contract

# COMMAND ----------

# MAGIC %run ./silver_cities

# COMMAND ----------

# MAGIC %run ./silver_quality

# COMMAND ----------

# MAGIC %run ./silver_bronze_legacy

# COMMAND ----------

# MAGIC %run ./silver_registry

# COMMAND ----------

# MAGIC %run ./silver_daily

# COMMAND ----------

# MAGIC %run ./silver_transform

# COMMAND ----------

# MAGIC %run ./silver_checks
