import matplotlib.pyplot as plt
import io

class FinanceService:
    @staticmethod
    def check_dynamic_budget(user_id: int, new_expense: float, category: str) -> str:
        """
        Calcula si un gasto supera el presupuesto.
        Si es así, genera un mensaje positivo de reasignación (El Fin de la 'Espiral de Culpa').
        """
        # TODO: Consultar DB para comparar presupuestos vs gastos
        return "¡Buen registro! Te pasaste un poco en ocio, pero ajustamos el límite de ropa de este mes para que sigas en carrera. ¡Vamos bien!"

    @staticmethod
    def generate_expense_chart(expenses_by_category: dict) -> bytes:
        """
        Genera un gráfico de torta básico y lo retorna como bytes.
        """
        labels = list(expenses_by_category.keys())
        sizes = list(expenses_by_category.values())
        
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90)
        ax.axis('equal')
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        
        plt.close(fig)
        return buf.read()
